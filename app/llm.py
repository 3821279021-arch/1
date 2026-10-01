from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import math
import time
from collections import deque
from contextlib import contextmanager, aclosing
from contextvars import ContextVar
import json
import os
import random
import re
from dataclasses import dataclass, field
from typing import Any
from statistics import median
from .tokens import estimate_tokens
from .providers import HTTPProviderAdapter, action_schema, provider_request, SecretStreamFilter, redact_secret
from .credentials import Credential, CredentialError, PROVIDER_URLS, check_public_endpoint, validate_model_id, validate_model_options, discover_models, pin_request
from .limits import Limits

import httpx

from .model_registry import ModelRegistry, ModelEntry

logger = logging.getLogger("werewolf.models")


@dataclass
class LLMResult:
    data: dict[str, Any]
    provider_used: str
    model_used: str
    raw: str = ""
    routing: dict[str, Any] = field(default_factory=dict)
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int = 0

    @property
    def text(self) -> str:
        return self.raw or json.dumps(self.data, ensure_ascii=False)

    @property
    def parsed(self) -> dict[str, Any]:
        return self.data

    @property
    def provider(self) -> str:
        return self.provider_used

    @property
    def model(self) -> str:
        return self.model_used


def extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass

    # 兼容 ```json ... ``` 或模型前后夹带解释文字的情况。
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


class LLMRouter:
    def __init__(self) -> None:
        self.timeout = float(os.getenv("LLM_TIMEOUT", "60"))
        self.semaphore = asyncio.Semaphore(max(1, int(os.getenv("LLM_CONCURRENCY", "4"))))
        self.registry = ModelRegistry()
        self.models = dict(self.registry.defaults)
        self.dashscope_url = (os.getenv("DASHSCOPE_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
        self.activity = {p: {"requests": 0, "successes": 0, "failures": 0, "fallbacks": 0, "cancelled": 0, "partial_streams": 0, "tokens": 0, "last_error": None} for p in self.models}
        self.retries = max(0, min(3, int(os.getenv("LLM_RETRIES", "1"))))
        self.retry_delay = max(0, min(1, float(os.getenv("LLM_RETRY_DELAY", "0.2"))))
        self.call_guard = None
        self.prices = json.loads(os.getenv("AI_MODEL_PRICES", "{}"))
        self.default_prices = {"input": float(os.getenv("AI_INPUT_PRICE_PER_MILLION", "0")), "output": float(os.getenv("AI_OUTPUT_PRICE_PER_MILLION", "0"))}
        self._scope: ContextVar[dict[str, Any]] = ContextVar(f"llm_scope_{id(self)}", default={})
        self._selection: ContextVar[ModelEntry | None] = ContextVar(f"llm_selection_{id(self)}", default=None)
        self._usage: ContextVar[dict[str, Any] | None] = ContextVar(f"llm_usage_{id(self)}", default=None)
        self.records: deque[dict[str, Any]] = deque(maxlen=5000)
        self.outcome_records: deque[dict[str, Any]] = deque(maxlen=5000)
        self._executions: dict[str, dict[str, Any]] = {}
        self._last_execution: dict[str, Any] = {}
        self.credentials = None
        self._credential: ContextVar[Credential | None] = ContextVar(f"llm_credential_{id(self)}", default=None)
        self._client: httpx.AsyncClient | None = None
        self.adapters = {p: HTTPProviderAdapter(p, self) for p in [*self.models, "openai-compatible"]}
        self._schema: ContextVar[dict | None] = ContextVar(f"llm_schema_{id(self)}", default=None)
        self.calibration: dict[str, deque] = {}
        self.safety_margin = max(1.0, min(1.2, float(os.getenv("TOKEN_SAFETY_MARGIN", "1.15"))))
        self.record_sink = None
        self._background_writes: set[asyncio.Task] = set()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, trust_env=False, limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
        return self._client

    async def close(self) -> None:
        if self._background_writes:
            await asyncio.gather(*self._background_writes, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()

    def cost_report(self, room_id: str, game_id: str, stored: dict | None = None) -> dict[str, Any]:
        records = stored["calls"] if stored is not None else [r for r in self.records if r.get("room_id") == room_id and r.get("game_id") == game_id]
        groups = {}
        for r in records:
            key = f"{r.get('phase')}:{r.get('category')}:{r.get('provider')}:{r.get('model')}"
            group = groups.setdefault(key, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost": 0.0, "failures": 0})
            group["calls"] += 1
            group["input_tokens"] += r.get("input_tokens", r["estimated_input_tokens"])
            group["output_tokens"] += r.get("output_tokens", 0)
            group["estimated_cost"] += r.get("estimated_cost", 0)
            group["failures"] += not r["success"]
        reasons = {}
        for r in stored["outcomes"] if stored is not None else self.outcome_records:
            if r.get("room_id") == room_id and r.get("game_id") == game_id and r.get("failure_reason"):
                reason = r["failure_reason"]
                reasons[reason] = reasons.get(reason, 0) + 1
        errors = [abs(r["estimated_actual_ratio"] - 1) for r in records if "estimated_actual_ratio" in r]
        return {"room_id": room_id, "game_id": game_id, "calls": len(records), "groups": groups,
                "fallback_reasons": reasons, "median_estimation_error": median(errors) if errors else None,
                "usage_samples": len(errors), "records": records,
                "window_limit": None if stored is not None else self.records.maxlen,
                "durable": stored is not None, "usage_note": "Missing input usage uses estimates; output totals exclude calls with missing output usage."}


    @contextmanager
    def request_scope(self, *, guard=None, **metadata):
        """Attach non-sensitive request IDs and a per-attempt cost guard without sharing prompts."""
        scope = {**self._scope.get(), **metadata}
        if guard is not None:
            scope["guard"] = guard
        token = self._scope.set(scope)
        try:
            yield
        finally:
            self._scope.reset(token)

    def status(self) -> dict[str, dict[str, Any]]:
        result = {}
        for provider, model in self.models.items():
            entry = self.registry.get(f"{provider}:{model}")
            public = entry.public() if entry else {"configured": False, "status": "unconfigured"}
            result[provider] = {key: public.get(key) for key in ("configured", "enabled", "healthy", "status")}
            result[provider]["model"] = model
        result["mock"] = {"configured": True, "enabled": True, "healthy": True, "status": "practice", "model": "rule-based-mock"}
        public_available = any(entry["configured"] and entry["enabled"] and entry["healthy"] for entry in self.registry.entries())
        result["auto"] = {"configured": public_available, "model": "automatic-real-model-pool", "status": "configured" if public_available else "unconfigured"}
        return result

    def model_status(self) -> list[dict[str, Any]]:
        return self.registry.entries()

    def execution(self, agent_id: str | None = None) -> dict[str, Any]:
        return copy.deepcopy(self._executions.get(agent_id, {}) if agent_id else self._last_execution)

    def _publish_execution(self, requested: str, used: str, chain: list[str], reason: str | None,
                           status: str, started: float, usage: dict[str, Any] | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        provider, model = used.split(":", 1)
        result = {"requested_model": requested, "model_used": model, "model_key": used,
                  "provider_used": provider, "fallback_chain": list(chain), "failure_reason": reason,
                  "status": status, "latency": round(time.monotonic()-started, 4), **(usage or {})}
        self._last_execution = result
        scope = metadata if metadata is not None else self._scope.get()
        if status != "streaming":
            outcome = {**{key: scope.get(key) for key in ("room_id", "game_id", "agent_id", "category", "day", "phase", "request_id")}, **result}
            self.outcome_records.append(outcome)
            if self.record_sink:
                task = asyncio.create_task(self.record_sink("route", copy.deepcopy(outcome)))
                self._background_writes.add(task)
                def completed(write):
                    self._background_writes.discard(write)
                    if not write.cancelled() and write.exception():
                        logger.warning("telemetry_storage_failed error=%s", type(write.exception()).__name__)
                task.add_done_callback(completed)
            logger.info("model_route %s", json.dumps(outcome, ensure_ascii=False))
        agent_id = scope.get("agent_id")
        if agent_id:
            self._executions[str(agent_id)] = result
            if len(self._executions) > 2048:
                self._executions.pop(next(iter(self._executions)))
        return copy.deepcopy(result)

    def _disclose_health(self, ctx: dict[str, Any]) -> bool:
        category = self._scope.get().get("category") or ctx.get("action")
        return category in {None, "speech", "vote", "probe", "health_probe"}

    def _model(self, provider: str) -> str:
        selected = self._selection.get()
        return selected.model if selected and selected.provider == provider else self.models[provider]

    def _capture_usage(self, body: dict[str, Any], provider: str) -> None:
        source = body.get("usage") or body.get("usageMetadata")
        if not isinstance(source, dict):
            return
        usage = {}
        mappings = {"input_tokens": ("input_tokens", "prompt_tokens"),
                    "output_tokens": ("output_tokens", "completion_tokens"), "total_tokens": ("total_tokens",)}
        if provider == "gemini":
            mappings = {"input_tokens": ("promptTokenCount",), "output_tokens": ("candidatesTokenCount",), "total_tokens": ("totalTokenCount",)}
        for destination, candidates in mappings.items():
            key = next((key for key in candidates if key in source), None)
            if key is not None:
                usage[destination] = int(source[key] or 0)
        if provider == "gemini":
            # Gemini can charge reasoning tokens separately from candidate text.
            if "total_tokens" in usage and "input_tokens" in usage:
                usage["output_tokens"] = max(usage.get("output_tokens", 0), usage["total_tokens"]-usage["input_tokens"])
            elif source.get("thoughtsTokenCount"):
                usage["output_tokens"] = usage.get("output_tokens", 0) + int(source["thoughtsTokenCount"])
        if provider == "anthropic" and "input_tokens" in usage:
            usage["input_tokens"] += int(source.get("cache_read_input_tokens", 0) or 0) + int(source.get("cache_creation_input_tokens", 0) or 0)
        slot = self._usage.get()
        if slot is not None:
            # Anthropic sends input and output usage in separate SSE events.
            slot.update(usage)
            if "input_tokens" in slot and "output_tokens" in slot and (provider == "anthropic" or "total_tokens" not in slot):
                slot["total_tokens"] = slot["input_tokens"] + slot["output_tokens"]

    async def _before_call(self, entry: ModelEntry, requested: str, ctx: dict[str, Any], system: str, user: str, stream: bool):
        scope = self._scope.get()
        meta = {key: scope.get(key) or ctx.get(key) for key in ("room_id", "game_id", "agent_id", "day", "phase", "request_id")}
        overhead = (19 if entry.model.startswith("qwen-turbo") else 15) if entry.provider == "dashscope" and entry.model.startswith("qwen") else 24
        base = await asyncio.to_thread(lambda: overhead + estimate_tokens(entry.provider, entry.model, system) + estimate_tokens(entry.provider, entry.model, user))
        schema = action_schema(ctx.get("action", ""), ctx.get("options", [])) if not stream else None
        if schema and entry.provider != "dashscope":
            base += estimate_tokens(entry.provider, entry.model, json.dumps(schema, ensure_ascii=False))
        samples = self.calibration.get(entry.key, [])
        coefficient = max(0.6, min(1.6, median(samples))) if len(samples) >= 3 else 1.0
        meta.update(base_input_tokens=base, calibration_coefficient=coefficient)
        options = validate_model_options(scope.get("model_parameters") or scope.get("model_options"))
        output_limit = options.get("max_output_tokens", 350 if stream else 600)
        if entry.provider == "anthropic" and options.get("enable_thinking") is not False:
            budget = options.get("thinking_budget", {"minimal": 1024, "low": 2048, "medium": 4096, "high": 8192, "xhigh": 16384}.get(options.get("reasoning_effort"), 2048 if options.get("enable_thinking") else 0))
            if budget:
                output_limit += max(1024, budget)
        meta.update(provider=entry.provider, model=entry.model, model_key=entry.key, requested_model=requested,
                    category=scope.get("category") or ctx.get("action") or ("speech" if stream else "json"),
                    max_output_tokens=output_limit, estimated_input_tokens=math.ceil(base * coefficient * self.safety_margin))
        guard = scope.get("guard") or self.call_guard
        ticket = None
        if guard is not None:
            ticket = await asyncio.to_thread(guard.before_call, meta) if isinstance(guard, Limits) else guard.before_call(meta)
            if inspect.isawaitable(ticket):
                ticket = await ticket
            if ticket is False:
                raise RuntimeError("BudgetExhausted")
        return guard, ticket, meta

    async def _after_call(self, guard, ticket, meta: dict[str, Any], *, started: float, success: bool,
                          reason: str | None = None, usage: dict[str, Any] | None = None):
        record = {**meta, "provider_used": meta.get("provider"), "model_used": meta.get("model"),
                  "success": success, "latency": round(time.monotonic()-started, 4),
                  "fallback_reason": reason, "failure_reason": reason, **(usage or {})}
        record["latency_ms"] = round(record["latency"] * 1000)
        if usage and usage.get("input_tokens", 0) > 0:
            actual = usage["input_tokens"]
            record["actual_input_tokens"] = actual
            record["estimated_actual_ratio"] = round(meta["estimated_input_tokens"] / actual, 4)
            self.calibration.setdefault(meta["model_key"], deque(maxlen=32)).append(actual / meta["base_input_tokens"])
        price = self.prices.get(str(meta.get("model_key"))) if isinstance(self.prices, dict) else None
        if isinstance(price, dict) or any(self.default_prices.values()):
            price = {**self.default_prices, **(price if isinstance(price, dict) else {})}
            input_tokens = (usage or {}).get("input_tokens", meta["estimated_input_tokens"])
            output_tokens = (usage or {}).get("output_tokens", meta["max_output_tokens"])
            try:
                in_price, out_price = float(price["input"]), float(price["output"])
                if usage and "total_tokens" in usage:
                    if "input_tokens" not in usage and "output_tokens" not in usage:
                        input_tokens, output_tokens = (usage["total_tokens"], 0) if in_price >= out_price else (0, usage["total_tokens"])
                    elif "input_tokens" not in usage:
                        input_tokens = max(0, usage["total_tokens"] - output_tokens)
                    elif "output_tokens" not in usage:
                        output_tokens = max(0, usage["total_tokens"] - input_tokens)
                if all(math.isfinite(value) and value >= 0 for value in (in_price, out_price)):
                    record["estimated_cost"] = round((input_tokens*in_price+output_tokens*out_price)/1_000_000, 8)
                    record["usage_estimated"] = not bool(usage) or "input_tokens" not in usage or "output_tokens" not in usage
            except (TypeError, ValueError, OverflowError):
                pass
        self.records.append(record)
        if self.record_sink:
            try:
                await self.record_sink("call", copy.deepcopy(record))
            except Exception as exc:
                logger.warning("telemetry_storage_failed error=%s", type(exc).__name__)
        logger.info("model_call %s", json.dumps(record, ensure_ascii=False))
        if guard is not None:
            settled = await asyncio.to_thread(guard.after_call, ticket, record) if isinstance(guard, Limits) else guard.after_call(ticket, record)
            if inspect.isawaitable(settled):
                await settled

    @staticmethod
    def retryable(exc: Exception) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code >= 500 or exc.response.status_code in {408, 409, 429}
        return isinstance(exc, (httpx.TransportError, ValueError, KeyError, IndexError, TypeError))

    @staticmethod
    def _reason(exc: BaseException) -> str:
        return f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__

    def note_failure(self, provider: str, exc: Exception) -> None:
        self.activity[provider]["failures"] += 1
        self.activity[provider]["last_error"] = self._reason(exc)

    @staticmethod
    def _valid_data(data: dict[str, Any], ctx: dict[str, Any]) -> bool:
        if not data:
            return False
        action = ctx.get("action")
        if action in {"speech", "pet", "wolf_discussion"}:
            return isinstance(data.get("speech"), str) and bool(data["speech"].strip())
        if action == "wolf_discuss":
            return isinstance(data.get("text"), str)
        if action == "optional_skill":
            return "action" in data and (data["action"] is None or isinstance(data["action"], str))
        if action not in {"vote", "wolf_kill", "seer_inspect", "witch", "guard_protect", "hunter_shoot", "knight_duel", "duel", "wolf_king_shoot", "wolf_beauty_charm", "self_destruct"}:
            return True
        aliases = {"vote": ("vote",), "wolf_kill": ("wolf_kill", "kill"), "seer_inspect": ("seer_inspect", "inspect"), "witch": ("witch",), "guard_protect": ("guard_protect", "guard", "protect"), "hunter_shoot": ("hunter_shoot", "shoot"), "knight_duel": ("knight_duel", "duel"), "duel": ("duel", "knight_duel"), "wolf_king_shoot": ("wolf_king_shoot", "shoot"), "wolf_beauty_charm": ("wolf_beauty_charm", "charm"), "self_destruct": ("self_destruct",)}[action]
        values = data
        fields = ("save", "poison_target") if action == "witch" else ("target",)
        for _ in range(3):
            if any(field in values for field in fields):
                break
            nested = next((values[key] for key in (*aliases, "action", "decision") if key in values), {})
            values = nested if isinstance(nested, dict) else {"target": nested}
        if action == "witch":
            return ("save" in values or "poison_target" in values) and ("save" not in values or type(values["save"]) is bool) and ("poison_target" not in values or values["poison_target"] is None or type(values["poison_target"]) is int and values["poison_target"] in ctx.get("options", []))
        target = values.get("target")
        if isinstance(target, str) and target.isdigit():
            target = int(target)
        return "target" in values and (target is None or type(target) is int and target in ctx.get("options", []))

    async def ask_json(self, provider: str, system_prompt: str, user_prompt: str, *, mock_context: dict[str, Any] | None = None, validator=None) -> LLMResult:
        ctx = mock_context or {}
        requested, candidates, bound, route_reason = self._route_candidates(provider, ctx, "json")
        chain: list[str] = []
        reason = None
        started = time.monotonic()
        denied = False
        reason = route_reason
        if not candidates and not requested.startswith("mock:"):
            reason = reason or "no_configured_healthy_models"
        for entry in candidates:
            method = self.adapters[entry.provider].generate
            repair = False
            for attempt in range(self.retries+1):
                chain.append(entry.key)
                attempt_user = user_prompt + ("\n上次输出无效。请仅返回符合当前动作 schema、合法目标和药剂条件的顶层 JSON。" if repair else "")
                try:
                    guard, ticket, meta = await self._before_call(entry, requested, ctx, system_prompt, attempt_user, False)
                except Exception as exc:
                    reason = f"limit:{getattr(exc, 'reason', type(exc).__name__)}"
                    denied = True
                    break
                meta["fallback_chain"] = list(chain)
                selection = self._selection.set(entry)
                credential_token = self._credential.set(bound)
                schema_token = self._schema.set(action_schema(ctx.get("action", ""), ctx.get("options", [])))
                usage: dict[str, Any] = {}
                usage_token = self._usage.set(usage)
                before = time.monotonic()
                success = False
                attempt_reason = None
                try:
                    async with self.semaphore:
                        self.activity[entry.provider]["requests"] += 1
                        result = await method(system_prompt, attempt_user)
                    if bound and not self.credentials.route_valid(bound.owner_id, bound.id, bound.revision, scope_id=bound.scope_id):
                        raise ValueError("Credential changed during request")
                    if not self._valid_data(result.data, ctx) or validator is not None and not validator(result.data):
                        raise ValueError("Invalid model JSON or action format")
                    success = True
                    attempt_reason = reason if entry.key != requested or attempt else None
                    self.activity[entry.provider]["successes"] += 1
                    self.activity[entry.provider]["tokens"] += usage.get("total_tokens", 0)
                    self.registry.succeeded(entry.key, disclose=self._disclose_health(ctx))
                    result.provider_used, result.model_used = entry.provider, entry.model
                    result.input_tokens, result.output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
                    result.latency_ms = round((time.monotonic() - before) * 1000)
                    result.routing = self._publish_execution(requested, entry.key, chain, reason, "switched" if entry.key != requested else "success", started, usage)
                    return result
                except asyncio.CancelledError:
                    attempt_reason = "cancelled"
                    self.activity[entry.provider]["cancelled"] += 1
                    raise
                except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                    repair = isinstance(exc, ValueError)
                    reason = attempt_reason = self._reason(exc)
                    self.note_failure(entry.provider, exc)
                    if not self.retryable(exc) or attempt == self.retries:
                        self.registry.failed(entry.key, reason, disclose=self._disclose_health(ctx))
                        break
                finally:
                    self._selection.reset(selection)
                    self._credential.reset(credential_token)
                    self._schema.reset(schema_token)
                    self._usage.reset(usage_token)
                    await self._after_call(guard, ticket, meta, started=before, success=success, reason=attempt_reason, usage=usage)
                if self.retry_delay:
                    await asyncio.sleep(self.retry_delay)
            if denied:
                break
        result = self._mock(ctx, error=(reason or "model_unavailable") if not requested.startswith("mock:") else None)
        if not requested.startswith("mock:"):
            requested_entry = self.registry.get(requested)
            if requested_entry:
                self.activity[requested_entry.provider]["fallbacks"] += 1
            result.provider_used = f"mock (fallback from {requested})"
        result.routing = self._publish_execution(requested, "mock:rule-based-mock", chain, reason,
                                                 "budget_exhausted" if denied else "mock_fallback" if not requested.startswith("mock:") else "practice", started)
        return result

    def _connection(self, provider: str) -> Credential:
        bound = self._credential.get()
        if bound is not None:
            if bound.provider != provider:
                raise CredentialError("模型供应商与凭据不匹配")
            return bound
        base = self.dashscope_url if provider == "dashscope" else PROVIDER_URLS[provider]
        return Credential("platform", "platform", provider, base, os.environ[f"{provider.upper()}_API_KEY"], "platform", "")

    def _route_candidates(self, provider: str, ctx: dict, capability: str):
        # This is transport metadata, never interpolated into either prompt.
        route = {**(ctx.get("_model_route") or {}), **self._scope.get()}
        credential_id = route.get("credential_id")
        if not credential_id:
            requested = self.registry.resolve(provider)
            return requested, self.registry.candidates(requested, capability), None, None
        model = route.get("model_id") or provider.split(":", 1)[-1]
        requested = provider
        try:
            if self.credentials is None:
                raise CredentialError("凭据服务不可用")
            bound = self.credentials.resolve(route.get("credential_owner_id"), credential_id, scope_id=route.get("credential_scope_id"))
            expected_revision = route.get("credential_revision")
            if expected_revision is not None and bound.revision != expected_revision:
                raise CredentialError("凭据已更新，请重新配置座位")
            model = validate_model_id(model)
            if bound.secret in model:
                raise CredentialError("模型 ID 不得包含 API Key")
            if bound.provider == "gemini":
                model = model.removeprefix("models/")
            requested = f"{bound.provider}:{model}"
            self.activity.setdefault(bound.provider, {"requests": 0, "successes": 0, "failures": 0, "fallbacks": 0, "cancelled": 0, "partial_streams": 0, "tokens": 0, "last_error": None})
            # BYOK has an isolated candidate pool, independent of platform keys.
            return requested, [ModelEntry(bound.provider, model, True)], bound, None
        except CredentialError:
            return requested, [], None, "credential_unavailable"

    async def refresh_platform_models(self) -> dict:
        results = {}
        for provider in self.models:
            secret = os.getenv(f"{provider.upper()}_API_KEY")
            if not secret:
                continue
            credential = self._connection(provider)
            try:
                models = await discover_models(credential, self.client)
                for model in models:
                    self.registry.register(provider, model["id"])
                results[provider] = {"verified": True, "models": models, "manual_entry_allowed": True}
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                results[provider] = {"verified": False, "models": [], "manual_entry_allowed": True,
                                     "error": "无法获取供应商模型列表，可配置手动模型 ID"}
        return results

    async def _provider_generate(self, provider: str, system_prompt: str, user_prompt: str) -> LLMResult:
        credential = self._connection(provider)
        address = await check_public_endpoint(credential.base_url)
        route = self._scope.get()
        url, headers, payload = provider_request(credential, self._model(provider), system_prompt, user_prompt,
                                                schema=self._schema.get(), max_tokens=600, parameters=route.get("model_parameters") or route.get("model_options"))
        url, headers, extensions = pin_request(url, headers, address)
        response = await self.client.post(url, headers=headers, json=payload, follow_redirects=False, extensions=extensions)
        response.raise_for_status()
        body = response.json()
        if body.get("error") or body.get("code"):
            raise ValueError("Provider response error")
        self._capture_usage(body, provider)
        data = None
        if provider == "openai":
            text = "".join(part.get("text", "") for item in body.get("output", []) if item.get("type") == "message"
                           for part in item.get("content", []) if part.get("type") == "output_text")
        elif provider == "anthropic":
            text = "".join(block.get("text", "") for block in body.get("content", []) if block.get("type") == "text")
            data = next((block.get("input") for block in body.get("content", []) if block.get("type") == "tool_use" and block.get("name") == "game_action"), None)
        elif provider == "gemini":
            text = "".join(part.get("text", "") for part in body.get("candidates", [{}])[0].get("content", {}).get("parts", []) if not part.get("thought"))
        else:
            source = body.get("output", body)
            text = source["choices"][0]["message"].get("content") or ""
        if not isinstance(text, str):
            raise ValueError("Invalid provider text content")
        text = redact_secret(text, credential.secret)
        data = redact_secret(data, credential.secret) if isinstance(data, dict) else extract_json(text)
        return LLMResult(data, provider, self._model(provider), text)

    async def _dashscope(self, system_prompt: str, user_prompt: str) -> LLMResult:
        return await self._provider_generate("dashscope", system_prompt, user_prompt)

    async def _openai(self, system_prompt: str, user_prompt: str) -> LLMResult:
        return await self._provider_generate("openai", system_prompt, user_prompt)

    async def _anthropic(self, system_prompt: str, user_prompt: str) -> LLMResult:
        return await self._provider_generate("anthropic", system_prompt, user_prompt)

    async def _gemini(self, system_prompt: str, user_prompt: str) -> LLMResult:
        return await self._provider_generate("gemini", system_prompt, user_prompt)

    async def _openai_compatible(self, system_prompt: str, user_prompt: str) -> LLMResult:
        return await self._provider_generate("openai-compatible", system_prompt, user_prompt)

    def _mock(self, ctx: dict[str, Any], error: str | None = None) -> LLMResult:
        if error:
            action = ctx.get("action", "speech")
            if action == "witch":
                data = {"save": False, "poison_target": None}
            elif action == "optional_skill":
                data = {"action": None, "target": None}
            elif action == "wolf_discuss":
                data = {"text": ""}
            elif action in {"speech", "pet", "wolf_discussion"}:
                data = {"speech": "模型连接失败，本轮跳过发言。"}
            else:
                data = {"target": None}
            data["fallback_error"] = error[:180]
            return LLMResult(data, "mock", "rule-based-mock", json.dumps(data, ensure_ascii=False))
        seed = int(ctx.get("seed", 1)) + int(ctx.get("seq", 0)) * 997 + int(ctx.get("player_id", 0)) * 31
        rng = random.Random(seed)
        action = ctx.get("action", "speech")
        alive = [int(x) for x in ctx.get("alive", [])]
        me = int(ctx.get("player_id", 0) or 0)
        candidates = ctx.get("options", [x for x in alive if x != me])
        if action in {"speech", "wolf_discussion", "pet"} and not candidates:
            candidates = [x for x in alive if x != me]

        if action in {"wolf_kill", "seer_inspect", "kill", "inspect", "poison", "vote", "guard_protect", "hunter_shoot", "knight_duel", "duel", "wolf_king_shoot", "wolf_beauty_charm"}:
            target = rng.choice(candidates) if candidates else None
            if action in {"wolf_kill", "kill"}:
                target = min(candidates) if candidates else None
            data = {"target": target, "reason": "基于当前公开发言和简单随机策略做出的判断。"}
        elif action in {"self_destruct", "optional_skill"}:
            data = {"action": None, "target": None} if action == "optional_skill" else {"target": None}
        elif action == "wolf_discuss":
            data = {"text": ""}
        elif action == "witch":
            killed = ctx.get("killed")
            save = bool(killed) and bool(ctx.get("can_save", ctx.get("antidote"))) and rng.random() < 0.45
            poison_candidates = [x for x in ctx.get("poison_options", candidates) if x != killed]
            poison = rng.choice(poison_candidates) if ctx.get("poison") and poison_candidates and rng.random() < 0.18 else None
            if save and not ctx.get("can_use_both", True):
                poison = None
            data = {"save": save, "poison_target": poison, "reason": "模拟女巫根据局势决定是否用药。"}
        elif action == "wolf_discussion":
            target = min(candidates) if candidates else None
            data = {"speech": f"建议先讨论 {target} 号，参考他的公开发言；最终选择阶段再确认。" if target else "当前没有合法夜杀目标。"}
        elif action == "pet":
            facts = ctx.get("facts", [])
            known = "；".join(str(f) for f in facts[-3:]) or "目前还没有可核实的公开证据"
            style = ctx.get("personality", "detective")
            suggestion = {
                "detective": "先核对发言与票型，追问怀疑依据。",
                "hunter": "建议主动追问最近发言的人：你的判断依据是什么？",
                "trickster": "可以用同一个问题试探不同人的回答，但不要编造查验。",
                "cautious": "证据不足时先保留判断，不把猜测当成身份。",
                "performer": "稳住！先让对方把依据说清楚，再表达你的看法。",
                "commander": "下一步：整理已知信息，追问依据，再比较票型。",
            }[style]
            question = str(ctx.get("question", ""))
            if "草稿" in question or "发言" in question:
                suggestion += " 发言草稿：『我会先核对公开信息，请大家说明各自的怀疑依据。』"
            data = {"speech": f"已知：{known}。{suggestion}目前的身份判断都只能算猜测。"}
            if ctx.get("advice_length") == "detailed":
                data["speech"] += "\n行动顺序：1.核对公开记录；2.追问票型与发言不一致之处；3.在你的合法回合内决定发言或投票。不要根据他人的自称直接确认身份。"
        else:
            susp = rng.choice(candidates) if candidates else me
            styles = {
                "detective": f"我先观察{susp}号，想核对他的发言与投票是否一致。这只是猜测，请说明判断依据。",
                "hunter": f"{susp}号，我想直接追问你：现在最怀疑谁，依据是什么？我会重点观察你的回答。",
                "trickster": f"我想试探{susp}号的站边理由。先听他的回答，再对比票型，暂时不认定身份。",
                "cautious": f"证据还不足，我对{susp}号保留观察，不能仅凭语气判断身份。",
                "performer": f"先稳住！{susp}号，把你的理由说清楚吧，我可不想稀里糊涂投票。",
                "commander": f"建议大家先交代怀疑和依据。我会观察{susp}号，投票后再统一核对逻辑。",
            }
            data = {"speech": styles.get(ctx.get("personality"), styles["detective"])}
            style = ctx.get("style", {})
            if style.get("aggression", 0) >= 0.8:
                data["speech"] = "我会主动追问。" + data["speech"]
            if style.get("caution", 0) >= 0.8:
                data["speech"] += "证据不足，先不下结论。"
            data["speech"] = data["speech"][:int(style.get("length", 120))]
        if error:
            data["fallback_error"] = error[:180]
        return LLMResult(data, "mock", "rule-based-mock", json.dumps(data, ensure_ascii=False))

    async def speech_stream(self, provider: str, system: str, user: str, ctx: dict[str, Any]):
        """Route native SSE without exposing reasoning or repeating an interrupted answer."""
        requested, candidates, bound, route_reason = self._route_candidates(provider, ctx, "stream")
        started = time.monotonic()
        chain: list[str] = []
        reason = None
        denied = False
        reason = route_reason
        if not candidates and not requested.startswith("mock:"):
            reason = reason or "no_configured_healthy_models"
        for entry in candidates:
            for attempt in range(self.retries+1):
                chain.append(entry.key)
                try:
                    guard, ticket, meta = await self._before_call(entry, requested, ctx, system, user, True)
                except Exception as exc:
                    reason = f"limit:{getattr(exc, 'reason', type(exc).__name__)}"
                    denied = True
                    break
                meta["fallback_chain"] = list(chain)
                usage: dict[str, Any] = {}
                before = time.monotonic()
                emitted = False
                success = False
                attempt_reason = None
                try:
                    async with self.semaphore:
                        self.activity[entry.provider]["requests"] += 1
                        async with aclosing(self.adapters[entry.provider].stream(system, user)) as native:
                            while True:
                                selection = self._selection.set(entry)
                                credential_token = self._credential.set(bound)
                                usage_token = self._usage.set(usage)
                                try:
                                    chunk = await anext(native)
                                except StopAsyncIteration:
                                    break
                                finally:
                                    self._selection.reset(selection)
                                    self._credential.reset(credential_token)
                                    self._usage.reset(usage_token)
                                if bound and not self.credentials.route_valid(bound.owner_id, bound.id, bound.revision, scope_id=bound.scope_id):
                                    raise ValueError("Credential changed during request")
                                if not chunk:
                                    continue
                                if not isinstance(chunk, str):
                                    raise ValueError("Invalid public text delta")
                                emitted = True
                                self._publish_execution(requested, entry.key, chain, reason, "streaming", started, usage)
                                yield chunk
                    if not emitted:
                        raise ValueError("Empty provider stream")
                    success = True
                    attempt_reason = reason if entry.key != requested or attempt else None
                    self.activity[entry.provider]["successes"] += 1
                    self.activity[entry.provider]["tokens"] += usage.get("total_tokens", 0)
                    self.registry.succeeded(entry.key, disclose=self._disclose_health(ctx))
                    self._publish_execution(requested, entry.key, chain, reason, "switched" if entry.key != requested else "success", started, usage)
                    return
                except (asyncio.CancelledError, GeneratorExit) as exc:
                    usage.pop("output_tokens", None)
                    usage.pop("total_tokens", None)
                    attempt_reason = "cancelled" if isinstance(exc, asyncio.CancelledError) else "stream_closed"
                    self.activity[entry.provider]["cancelled"] += 1
                    self._publish_execution(requested, entry.key, chain, attempt_reason, "cancelled", started, usage, metadata=meta)
                    raise
                except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                    reason = attempt_reason = self._reason(exc)
                    self.note_failure(entry.provider, exc)
                    if emitted:
                        usage.pop("output_tokens", None)
                        usage.pop("total_tokens", None)
                        self.activity[entry.provider]["partial_streams"] += 1
                        self.registry.failed(entry.key, reason, disclose=self._disclose_health(ctx))
                        self._publish_execution(requested, entry.key, chain, reason, "partial", started, usage)
                        return
                    if not self.retryable(exc) or attempt == self.retries:
                        self.registry.failed(entry.key, reason, disclose=self._disclose_health(ctx))
                        break
                finally:
                    await self._after_call(guard, ticket, meta, started=before, success=success, reason=attempt_reason, usage=usage)
                if self.retry_delay:
                    await asyncio.sleep(self.retry_delay)
            if denied:
                break
        if not requested.startswith("mock:"):
            entry = self.registry.get(requested)
            if entry:
                self.activity[entry.provider]["fallbacks"] += 1
        self._publish_execution(requested, "mock:rule-based-mock", chain, reason,
                                "budget_exhausted" if denied else "mock_fallback" if not requested.startswith("mock:") else "practice", started)
        text = self._mock(ctx, error=(reason or "model_unavailable") if not requested.startswith("mock:") else None).data["speech"]
        for offset in range(0, len(text), 6):
            yield text[offset:offset+6]
            await asyncio.sleep(float(os.getenv("AI_CHUNK_DELAY", "0.09")))

    async def _native_speech(self, provider: str, system: str, user: str):
        credential = self._connection(provider)
        address = await check_public_endpoint(credential.base_url)
        url, headers, payload = provider_request(credential, self._model(provider), system, user,
                                                stream=True, max_tokens=350, parameters=self._scope.get().get("model_parameters") or self._scope.get().get("model_options"))
        url, headers, extensions = pin_request(url, headers, address)
        redactor = SecretStreamFilter(credential.secret)
        async with self.client.stream("POST", url, headers=headers, json=payload, follow_redirects=False, extensions=extensions) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                body = json.loads(raw)
                if provider == "anthropic" and body.get("type") == "message_start":
                    self._capture_usage(body.get("message", {}), provider)
                elif provider == "openai" and body.get("type") == "response.completed":
                    self._capture_usage(body.get("response", {}), provider)
                else:
                    self._capture_usage(body, provider)
                if body.get("type") == "error" or "error" in body or body.get("code"):
                    raise ValueError("Provider stream error")
                pieces = []
                if provider == "openai" and body.get("type") == "response.output_text.delta":
                    pieces.append(body.get("delta", ""))
                elif provider == "anthropic" and body.get("type") == "content_block_delta" and body.get("delta", {}).get("type") == "text_delta":
                    pieces.append(body["delta"].get("text", ""))
                elif provider == "gemini":
                    for candidate in body.get("candidates", []):
                        for part in candidate.get("content", {}).get("parts", []):
                            if not part.get("thought") and part.get("text"):
                                pieces.append(part["text"])
                elif provider in {"dashscope", "openai-compatible"}:
                    for choice in body.get("output", body).get("choices", []):
                        content = choice.get("delta", choice.get("message", {})).get("content")
                        if isinstance(content, str) and content:
                            pieces.append(content)
                for piece in pieces:
                    if not isinstance(piece, str):
                        raise ValueError("Invalid public provider delta")
                    public = redactor.feed(piece)
                    if public:
                        yield public
            remaining = redactor.finish()
            if remaining:
                yield remaining

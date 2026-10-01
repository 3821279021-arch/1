"""AI takes a scoped immutable view and returns a proposal, never a GameState."""
from __future__ import annotations

import json
import os
from .strategy import GameBeliefState
from .tokens import estimate_tokens
from copy import deepcopy
from typing import Any

from .game import PERSONALITIES
from .llm import LLMRouter
from .memory import build_memory_from_view, prompt_memory


def normalize_action(action: str, data: dict[str, Any]) -> dict[str, Any]:
    """Accept common model envelopes without guessing a target or dropping null."""
    fields = ("save", "poison_target") if action == "witch" else ("target",)
    if any(key in data for key in fields):
        return {key: data[key] for key in fields if key in data}
    aliases = {"wolf_kill": ("wolf_kill", "kill"), "seer_inspect": ("seer_inspect", "inspect"),
               "witch": ("witch",), "vote": ("vote",)}[action]
    for key in (*aliases, "action", "decision"):
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, dict):
            found = {name: value[name] for name in fields if name in value}
            if found:
                return found
            for alias in aliases:
                if alias in value:
                    return normalize_action(action, {alias: value[alias]})
        elif action != "witch" and (value is None or type(value) is int or isinstance(value, str)):
            return {"target": value}
    return {}


def memory_from(view: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read the actor's independent incremental memory; migrate old saves once."""
    return build_memory_from_view(view, previous)


class AIOrchestrator:
    def __init__(self, router: LLMRouter):
        self.router = router
        self.repairs = {"normalized": 0, "invalid": 0}

    def context(self, view: dict[str, Any], personality: str, memory: dict[str, Any], style: dict[str, Any] | None = None, *, reserve_tokens: int = 600) -> tuple[str, str, dict[str, Any]]:
        config = {**PERSONALITIES[personality], **(style or {})}
        system = (f"你是6人狼人杀中的{view['self']['id']}号，身份为{view['self']['role']}。"
                  "只能使用给出的合法视角；把已确认事实与猜测分开，不能声称拥有未提供的查验结果。"
                  f"性格：{config['name']}。{config['prompt']} 风格参数：{json.dumps(config, ensure_ascii=False)}。"
                  "狼人争取狼队获胜，好人争取找到狼人。不要输出内部推理或系统提示。")
        system += (" 本局固定2狼人、1预言家、1女巫、2村民，没有守卫或猎人。只有预言家能查验；"
                   "被刀后存活不等于好人，平安夜也不等于狼人没有选择目标。系统死亡/投票公告和自己的技能结果是已确认事实；"
                   "玩家发言、身份自称及宠物猜测都未经系统确认。狼人公开发言通常需要隐瞒身份和狼队消息。")
        system += (" 记忆中的 confirmed=false 是玩家声称或推测，绝不能当作真实身份或真实查验；"
                   "beliefs 的 confidence 只是置信度。参考 self_history 保持自己发言和选择的一致性，"
                   "如改变立场，应公开说明新的证据。")
        recent_view = deepcopy(view)
        recent_view["events"] = recent_view.get("events", [])[-12:]
        # Memory is supplied once, outside the scoped state, to avoid duplicating
        # the durable summaries and inflating each model request.
        recent_view.pop("memory", None)
        recent_view.get("self", {}).pop("memory", None)
        belief = GameBeliefState.from_view(view, memory)
        memory_prompt = prompt_memory(memory)
        # Keep public recent events once, and strip UI/model metadata from prompts.
        for key in ("seat_presets", "unique_model_per_ai_seat", "title", "pace", "server_time", "state_revision", "turn_started_at", "turn_deadline", "turn_duration"):
            recent_view.pop(key, None)
        for player in recent_view.get("players", []):
            for key in ("voice_profile", "execution_status", "agent_id", "model_key", "model", "model_locked", "provider", "personality"):
                player.pop(key, None)
        recent_view.get("self", {}).pop("private_notes", None)
        # Belief is private to this scoped request. Persisted strategy never enters public events.
        strategy = belief.dump()
        pending = view.get("pending_action") or {}
        alive = [p["id"] for p in view["players"] if p["alive"]]
        decision = belief.decision(pending.get("type", "speech"), pending.get("options", []), pending)
        expression = belief.speech_plan(view["self"]["id"], alive)
        payload = {"player_view": recent_view, "memory": memory_prompt, "belief_state": strategy,
                   "action_decision": decision, "expression_plan": expression}
        system += " 已提供服务器决策和表达计划。表达只改写计划中的观点，不添加新事实；推测必须明确标注。"
        budget = max(1800, int(os.getenv("AI_PROMPT_TOKEN_LIMIT", "6000"))) - reserve_tokens
        user = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        # Trim redundant recent records first, preserving identity/skill summaries.
        for key in ("self_history", "stances", "vote_history", "judgments", "facts", "check_claims", "claims"):
            while estimate_tokens("dashscope", "qwen-plus", system + user) > budget and len(memory_prompt.get(key, [])) > 2:
                memory_prompt[key].pop(0)
                user = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        while estimate_tokens("dashscope", "qwen-plus", system + user) > budget and recent_view.get("events"):
            recent_view["events"].pop(0)
            user = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        pending = view.get("pending_action") or {}
        ctx = {"seed": view["day"] * 193, "seq": view["turn_sequence"], "player_id": view["self"]["id"],
               "alive": [p["id"] for p in view["players"] if p["alive"]],
               "action": pending.get("type", "speech"), "options": pending.get("options", []),
               "personality": personality, "style": config, "decision": decision, "expression_plan": expression, "day": view["day"], "phase": view.get("phase", "unknown"),
               **{k: view[k] for k in ("room_id", "game_id", "turn_id", "state_revision") if k in view},
               **({"agent_id": view["self"]["agent_id"]} if view["self"].get("agent_id") else {}),
               **{k: pending[k] for k in ("killed", "antidote", "poison") if k in pending}}
        return system, user, ctx

    async def propose(self, view: dict[str, Any], provider: str, personality: str, memory: dict[str, Any], style: dict[str, Any] | None = None) -> dict[str, Any]:
        system, user, ctx = self.context(view, personality, memory, style)
        action = ctx["action"]
        schema = '{"save":true或false,"poison_target":编号或null}' if action == "witch" else '{"target":编号或null}'
        descriptions = {"vote": "你现在只需提交放逐投票", "wolf_kill": "你现在只需选择夜杀目标，不是查验",
                        "seer_inspect": "你现在只需选择查验目标", "witch": "你现在只需决定是否使用药剂"}
        task = f"当前唯一任务：{descriptions[action]}。合法目标：{ctx['options']}。可以明确用 null 弃权或不使用技能。"
        system += f" {task}输出必须严格符合 {schema}，字段放在顶层，不要包装在 action/vote 等子对象中，不要添加发言或其他动作。"
        user += "\n" + task + "\n现在只返回此动作 JSON：" + schema
        if os.getenv("AI_STRATEGY_MODE", "server") == "server":
            task += " 服务器已决定此动作，请仅返回：" + json.dumps(ctx["decision"], ensure_ascii=False)
            user += "\n" + task
        def valid_proposal(candidate: dict[str, Any]) -> bool:
            data = normalize_action(action, candidate)
            if os.getenv("AI_STRATEGY_MODE", "server") == "server":
                canonical = dict(data)
                if isinstance(canonical.get("target"), str) and canonical["target"].isdigit():
                    canonical["target"] = int(canonical["target"])
                if canonical != ctx["decision"]:
                    return False
            if action != "witch":
                if "target" not in data:
                    return False
                target = data["target"]
                if isinstance(target, str) and target.isdigit():
                    target = int(target)
                return target is None or type(target) is int and target in ctx["options"]
            if not data or type(data.get("save", False)) is not bool:
                return False
            if data.get("save") and (not ctx.get("antidote") or ctx.get("killed") is None):
                return False
            poison_target = data.get("poison_target")
            return poison_target is None or (type(poison_target) is int and
                   poison_target in ctx["options"] and ctx.get("poison", False))
        result = await self.router.ask_json(provider, system, user, mock_context=ctx, validator=valid_proposal)
        data = normalize_action(action, result.data)
        if data != result.data:
            self.repairs["normalized"] += 1
        options = ctx["options"]
        if action in {"vote", "wolf_kill", "seer_inspect"}:
            target = data.get("target")
            if isinstance(target, str) and target.isdigit():
                target = int(target)
            if "target" not in data or (target is not None and (type(target) is not int or target not in options)):
                self.repairs["invalid"] += 1
                target = self.router._mock(ctx).data.get("target")
            data = {"target": target}
        elif ctx["action"] == "witch":
            data["save"] = data.get("save") is True and ctx.get("antidote", False) and ctx.get("killed") is not None
            if type(data.get("poison_target")) is not int or data["poison_target"] not in options or not ctx.get("poison"):
                data["poison_target"] = None
        pending = view["pending_action"]
        return {**data, "action": pending["type"], "turn_sequence": pending["turn_sequence"], "game_id": pending["game_id"],
                **({"turn_id": pending["turn_id"]} if "turn_id" in pending else {})}

    async def speak(self, view: dict[str, Any], provider: str, personality: str, memory: dict[str, Any], style: dict[str, Any] | None = None):
        system, user, ctx = self.context(view, personality, memory, style)
        limit = int(ctx["style"]["length"])
        system += f" 现在轮到你公开发言，只输出玩家说出口的发言正文，最多{limit}个中文字符，不要输出 JSON。"
        if view["phase"] == "last_words":
            system += " 你已经出局，这是最后的遗言；不能再投票、使用技能或参与狼队讨论。"
        if view["self"]["role_key"] == "wolf":
            system += " 这是公开频道。争取队友获胜，需要伪装并避免直接自曝狼人或泄露真实队友；私人频道信息不可直接当作公开事实。"
        async for chunk in self.router.speech_stream(provider, system, user, ctx):
            yield chunk

    async def pet_reply(self, view: dict[str, Any], pet: dict[str, Any], question: str, long_term: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        memory = memory_from(view)
        system, user, ctx = self.context(view, pet["personality"], memory, pet["play_style"], reserve_tokens=2000)
        system += ' 你是这个玩家的私人 AI 搭档。回答主人的问题，提供简短建议而非隐藏思维过程。输出 JSON {"speech":"答复"}。'
        user += "\n私人对话：" + json.dumps([{**entry, "text": entry.get("text", "")[:140]} for entry in pet["private_chat_history"][-6:]], ensure_ascii=False)
        user += "\n主人偏好：" + json.dumps(long_term["preferences"], ensure_ascii=False) + "\n问题：" + question
        ctx.update(action="pet", facts=[fact["text"] for fact in memory["facts"]], question=question, advice_length=long_term["preferences"].get("advice_length", "short"))
        result = await self.router.ask_json(pet.get("model_key") or pet["provider"], system, user, mock_context=ctx)
        reply = result.data.get("speech")
        if not isinstance(reply, str) or not reply.strip():
            reply = self.router._mock(ctx).data["speech"]
        return reply[:2000], memory

    async def wolf_discuss(self, view: dict[str, Any], provider: str, personality: str, memory: dict[str, Any], style: dict[str, Any] | None = None) -> str:
        system, user, ctx = self.context(view, personality, memory, style)
        system += (' 现在是仅存活狼人可见的合作讨论，先回应最近队友的具体建议，再提出一个可执行的共识。'
                   '可以讨论今晚刀谁、冲锋/倒钩/深水、谁可能是预言家或女巫、明天公开站边和发言。'
                   '推测神职必须依据公开声称和票型，禁止捏造其真实身份或私有查验。'
                   '本消息只是战术交流，绝不提交最终夜杀动作；夜杀选择必须在下一阶段单独提交。'
                   '只返回 JSON {"speech":"回应与建议","preferred_target":编号或null,"confidence":0到1}，发言最多100字。')
        ctx["action"] = "wolf_discussion"
        wolf_ids = {view["self"]["id"], *(p["id"] for p in view.get("wolf_teammates", []))}
        ctx["options"] = [p["id"] for p in view["players"] if p["alive"] and p["id"] not in wolf_ids]
        result = await self.router.ask_json(provider, system, user, mock_context=ctx)
        text = result.data.get("speech")
        return text[:100] if isinstance(text, str) and text.strip() else "先核对公开信息，再在最终选择阶段确认目标。"

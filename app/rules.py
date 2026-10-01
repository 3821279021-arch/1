"""Authoritative state machine; AI and HTTP handlers only propose commands."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import random
import time
import logging
import json
from typing import Any

from .game import PERSONALITIES, PHASE_NAMES, PROVIDERS, TIMINGS, PetAI, Player, WerewolfGame

log = logging.getLogger("werewolf.lifecycle")


class RuleEngine:
    def __init__(self, game: WerewolfGame, *, time_scale: float = 1) -> None:
        self.g = game
        self.time_scale = time_scale
        self.outbox: list[dict[str, Any]] = []

    def emit(self, kind: str, data: dict[str, Any] | None = None, audience: str = "public", pid: int | None = None, *, mutation: bool = True) -> dict[str, Any]:
        g = self.g
        if mutation:
            g.state_revision += 1
            g.updated_at = time.time()
        g.event_seq += 1
        payload = deepcopy(data or {})
        payload.update(event_id=f"{g.game_id}:{g.event_seq}", event_seq=g.event_seq,
                       state_revision=g.state_revision, turn_id=g.turn_id)
        event = {"type": kind, "data": payload, "audience": audience, "player_id": pid,
                 "game_id": g.game_id, "turn_id": g.turn_id, "turn_sequence": g.turn_sequence,
                 "state_revision": g.state_revision, "event_id": payload["event_id"],
                 "seq": g.event_seq, "day": g.day}
        self.outbox.append(event)
        if kind in {"phase_changed", "game_finished", "room_closed", "room_moderated"}:
            log.info("lifecycle %s", json.dumps({"event": kind, "room_id": g.room_id,
                     "game_id": g.game_id, "day": g.day, "phase": g.phase,
                     "request_id": event["event_id"]}))
        # Every agent receives only the events it is legally entitled to know.
        from .scope import InformationScope
        from .memory import IGNORED_EVENTS, update_memory
        if kind not in IGNORED_EVENTS:
            for player in g.players:
                if InformationScope.permits_player(g, player.id, event):
                    player.memory = update_memory(player.memory, event, player.id)
        return payload

    def record(self, kind: str, text: str, pid: int | None = None, **extra: Any) -> None:
        self.g.seq += 1
        event = {"seq": self.g.seq, "day": self.g.day, "kind": kind, "text": text, "player_id": pid, **extra}
        event = self.emit("chat_message", event)
        self.g.events.append(event)

    def join(self, owner_id: str, name: str, seat: int) -> Player:
        g = self.g
        if g.phase != "lobby":
            raise ValueError("游戏已经开始，暂不能加入")
        if g.owned_player(owner_id):
            raise ValueError("你已经在房间中")
        existing = next((p for p in g.players if p.id == seat), None)
        if not 1 <= seat <= 6 or (existing is not None and existing.owner_id):
            raise ValueError("座位不可用")
        # AI settings fill the lobby roster; a human may take that planned seat.
        if existing is not None:
            g.players.remove(existing)
        p = Player(seat, name[:24] or "玩家", owner_id=owner_id)
        g.players.append(p)
        g.players.sort(key=lambda p: p.id)
        g.pets[owner_id] = PetAI()
        self.record("system", f"{p.name} 加入了 {seat} 号座位。")
        log.info("lifecycle %s", json.dumps({"event": "player_joined", "room_id": g.room_id, "agent_id": p.agent_id, "request_id": f"{g.game_id}:{g.event_seq}"}))
        return p

    def configure(self, owner_id: str, pace: str, seats: list[dict[str, Any]], unique_model_per_ai_seat: bool | None = None) -> None:
        if owner_id != self.g.host_id or self.g.phase != "lobby":
            raise ValueError("只有房主可以在开始前配置房间")
        if pace not in TIMINGS:
            raise ValueError("未知速度")
        if unique_model_per_ai_seat is not None and type(unique_model_per_ai_seat) is not bool:
            raise ValueError("独立模型设置必须为布尔值")
        if len({entry.get("id") for entry in seats}) != len(seats):
            raise ValueError("座位设置重复")
        for entry in seats:
            if type(entry.get("id")) is not int or not 1 <= entry["id"] <= 6:
                raise ValueError("非法座位")
            if entry.get("provider", "mock") not in PROVIDERS or entry.get("personality", "detective") not in PERSONALITIES:
                raise ValueError("未知模型或性格")
            existing = next((p for p in self.g.players if p.id == entry["id"]), None)
            if existing and existing.owner_id:
                raise ValueError("真人座位不能配置成 AI")
            if not isinstance(entry.get("model_key", ""), str) or len(entry.get("model_key", "")) > 180:
                raise ValueError("模型标识无效")
            if type(entry.get("model_locked", False)) is not bool:
                raise ValueError("模型锁定设置无效")
        self.g.pace = pace
        if unique_model_per_ai_seat is not None:
            self.g.unique_model_per_ai_seat = unique_model_per_ai_seat
        for entry in seats:
            self.g.seat_presets[str(entry["id"])] = {"id": entry["id"], "provider": entry.get("provider", "mock"), "personality": entry.get("personality", "detective"), "model_key": entry.get("model_key", ""), "model_locked": entry.get("model_locked", False)}
        self.emit("lobby_configured", {})

    def start(self, owner_id: str, now: float | None = None, model_assignments: dict[int, str] | None = None) -> None:
        if owner_id != self.g.host_id or self.g.phase != "lobby":
            raise ValueError("只有房主可以开始未开局的房间")
        for seat in range(1, 7):
            if not any(p.id == seat for p in self.g.players):
                preset = self.g.seat_presets.get(str(seat), {})
                key = (model_assignments or {}).get(seat) or preset.get("model_key") or "mock:mock"
                provider, model = key.split(":", 1)
                self.g.players.append(Player(seat, f"AI {seat}号", provider=provider, model=model, model_key=key,
                    model_locked=preset.get("model_locked", False), personality=preset.get("personality", list(PERSONALITIES)[seat-1]),
                    voice_profile={"voice_id": "", "rate": 0.9 + seat*0.035, "pitch": 0.8+seat*0.08, "volume": 0.8}))
        self.g.players.sort(key=lambda p: p.id)
        roles = ["wolf", "wolf", "seer", "witch", "villager", "villager"]
        random.SystemRandom().shuffle(roles)
        for p, role in zip(self.g.players, roles):
            p.role = role
        self.g.lifecycle = "ACTIVE"
        self.record("system", "身份牌已私下发放。天黑请闭眼。")
        self.enter("night_discussion", now=now)

    def enter(self, phase: str, pid: int | None = None, now: float | None = None) -> None:
        g = self.g
        g.phase = phase
        g.current_turn_player_id = pid
        g.turn_sequence += 1
        g.turn_started_at = time.time() if now is None else now
        g.turn_duration = TIMINGS[g.pace].get(phase, 0) * self.time_scale
        g.turn_deadline = g.turn_started_at + g.turn_duration if g.turn_duration else None
        g.submitted = []
        g.current_speech = ""
        if phase == "day_vote":
            g.votes = {}
        self.emit("phase_changed", {"phase": phase, "phase_name": PHASE_NAMES[phase], "current_turn_player_id": pid if phase in {"day_speech", "last_words"} else None, "turn_deadline": g.turn_deadline, "turn_duration": g.turn_duration})
        self.emit("vote_started" if phase == "day_vote" else "turn_started")

    def action_for(self, pid: int) -> dict[str, Any] | None:
        g = self.g
        p = g.player(pid)
        if g.lifecycle != "ACTIVE" or g.game_over or (not p.alive and not (g.phase == "last_words" and pid == g.current_turn_player_id)) or pid in g.submitted:
            return None
        targets = [x for x in g.alive_ids() if x != pid]
        action = None
        extra: dict[str, Any] = {}
        if g.phase == "night_wolves" and p.role == "wolf":
            action = "wolf_kill"
            targets = [q.id for q in g.alive_players() if q.role != "wolf"]
        elif g.phase == "night_seer" and p.role == "seer":
            action = "seer_inspect"
        elif g.phase == "night_witch" and p.role == "witch":
            action = "witch"
            extra = {"killed": g.night_kill if g.witch_antidote else None, "antidote": g.witch_antidote, "poison": g.witch_poison}
        elif g.phase in {"day_speech", "last_words"} and pid == g.current_turn_player_id:
            action = "speech"
        elif g.phase == "day_vote" and str(pid) not in g.votes:
            action = "vote"
        if not action:
            return None
        return {"type": action, "options": targets, "turn_sequence": g.turn_sequence, "game_id": g.game_id, "turn_id": g.turn_id, "state_revision": g.state_revision, **extra}

    def validate(self, pid: int, payload: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        g = self.g
        now = time.time() if now is None else now
        if payload.get("game_id") != g.game_id or payload.get("turn_sequence") != g.turn_sequence:
            raise ValueError("回合已变更，请刷新当前状态")
        if payload.get("turn_id") is not None and payload["turn_id"] != g.turn_id:
            raise ValueError("回合已结束，请同步状态")
        if payload.get("expected_state_revision") is not None and payload["expected_state_revision"] != g.state_revision:
            raise ValueError("状态版本已变更，请同步后重试")
        if g.turn_deadline is None or now >= g.turn_deadline:
            raise ValueError("回合已超时")
        pending = self.action_for(pid)
        if not pending or payload.get("action") != pending["type"]:
            raise ValueError("当前不能执行此动作或已提交")
        def target_valid(value: Any) -> bool:
            return value is None or (type(value) is int and value in pending["options"])
        if pending["type"] in {"vote", "wolf_kill", "seer_inspect"} and not target_valid(payload.get("target")):
            raise ValueError("目标不合法")
        if pending["type"] == "speech" and (not isinstance(payload.get("speech", ""), str) or len(payload.get("speech", "")) > 500):
            raise ValueError("发言最多 500 字")
        if pending["type"] == "witch":
            if type(payload.get("save", False)) is not bool:
                raise ValueError("解药选择无效")
            if payload.get("save") and (not g.witch_antidote or g.night_kill is None):
                raise ValueError("解药不可用")
            if not target_valid(payload.get("poison_target")) or (payload.get("poison_target") is not None and not g.witch_poison):
                raise ValueError("毒药不可用或目标不合法")
        return pending

    def apply(self, pid: int, payload: dict[str, Any], now: float | None = None) -> None:
        pending = self.validate(pid, payload, now)
        g, p = self.g, self.g.player(pid)
        notes_before = len(p.private_notes)
        action = pending["type"]
        target = payload.get("target")
        if action == "wolf_kill":
            g.night_choices[str(pid)] = target
            p.private_notes.append(f"第{g.day}夜，你的夜杀选择为 {target or '弃权'}；这不是查验，不确认目标身份。")
        elif action == "seer_inspect" and target is not None:
            alignment = "狼人" if g.player(target).role == "wolf" else "好人"
            p.private_notes.append(f"第{g.day}夜查验：{target}号是{alignment}。")
        elif action == "witch":
            if payload.get("save"):
                p.private_notes.append(f"第{g.day}夜你救了 {g.night_kill} 号。")
                g.night_kill = None
                g.witch_antidote = False
            if payload.get("poison_target") is not None:
                g.night_poison = payload["poison_target"]
                g.witch_poison = False
                p.private_notes.append(f"第{g.day}夜你毒了 {g.night_poison} 号。")
        elif action == "speech":
            speech = payload.get("speech", "").strip() or g.current_speech or "（结束发言）"
            self.record("speech", f"{pid}号 {p.name}：{speech}", pid, speech=speech)
            self.emit("speech_finished", {"player_id": pid, "speech": speech, "content": speech})
            g.submitted.append(pid)
            self.emit("turn_finished", {"player_id": pid})
            self.advance_speech(now)
            return
        elif action == "vote":
            g.votes[str(pid)] = target
            self.emit("vote_submitted", {"player_id": pid})
        g.submitted.append(pid)
        # Hidden actions and their timing are visible only to the actor.
        self.emit("player_action", {"action": action, "target": target, "save": payload.get("save", False), "poison_target": payload.get("poison_target"), "private_notes": p.private_notes[notes_before:]}, "player", pid)
        if action == "vote" and len(g.votes) == len(g.alive_ids()):
            self.resolve_vote(now)

    def append_speech(self, pid: int, chunk: str, sequence: int, now: float | None = None) -> bool:
        g = self.g
        if g.turn_sequence != sequence or not self.action_for(pid) or g.phase not in {"day_speech", "last_words"}:
            return False
        if (time.time() if now is None else now) >= (g.turn_deadline or 0):
            return False
        chunk = chunk[:max(0, 500-len(g.current_speech))]
        if not g.current_speech:
            self.emit("speech_started", {"player_id": pid})
        g.current_speech += chunk
        self.emit("speech_chunk", {"player_id": pid, "chunk": chunk, "delta": chunk})
        return True

    def advance_speech(self, now: float | None = None) -> None:
        g = self.g
        if g.phase == "last_words":
            if g.last_words_queue:
                self.enter("last_words", g.last_words_queue.pop(0), now)
            elif g.after_last_words == "day_speech":
                self.begin_day(now)
            else:
                self.next_night(now)
        elif g.speech_queue:
            self.enter("day_speech", g.speech_queue.pop(0), now)
        else:
            self.record("system", "所有玩家发言完毕，开始投票。提交后仅显示已投状态，结束时公布票型。")
            self.enter("day_vote", now=now)

    def begin_day(self, now: float | None = None) -> None:
        self.g.speech_queue = self.g.alive_ids()
        self.enter("day_speech", self.g.speech_queue.pop(0), now)

    def wolf_message(self, pid: int, text: str) -> None:
        p = self.g.player(pid)
        if not p.alive or p.role != "wolf" or self.g.phase not in {"night_discussion", "night_wolves"}:
            raise ValueError("只有存活狼人可以在夜间使用狼队频道")
        if not text.strip() or len(text) > 500:
            raise ValueError("消息需为 1 到 500 字")
        entry = self.emit("wolf_chat_message", {"player_id": pid, "day": self.g.day, "text": text, "seq": len(self.g.wolf_chat)+1}, "wolves")
        self.g.wolf_chat.append(entry)

    def tick(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        g = self.g
        if g.turn_deadline is None or now < g.turn_deadline or g.game_over:
            return False
        phase = g.phase
        if phase == "night_discussion":
            g.night_choices = {}
            self.enter("night_wolves", now=now)
        elif phase == "night_wolves":
            votes = Counter(t for t in g.night_choices.values() if t is not None)
            # A strict majority is required; abstentions count. A tie means no kill.
            majority = len([p for p in g.alive_players() if p.role == "wolf"]) // 2 + 1
            g.night_kill = next((target for target, count in votes.items() if count >= majority), None)
            self.enter("night_seer", now=now)
        elif phase == "night_seer":
            self.enter("night_witch", now=now)
        elif phase == "night_witch":
            self.resolve_dawn(now)
        elif phase in {"day_speech", "last_words"}:
            pid = g.current_turn_player_id
            self.record("speech", f"{pid}号：{g.current_speech or '（发言超时）'}", pid, speech=g.current_speech or "（发言超时）")
            self.emit("speech_finished", {"player_id": pid, "speech": g.current_speech, "content": g.current_speech or "（发言超时）", "partial": True})
            self.emit("turn_finished", {"player_id": pid, "timeout": True})
            self.advance_speech(now)
        elif phase == "day_vote":
            for pid in g.alive_ids():
                g.votes.setdefault(str(pid), None)
            self.resolve_vote(now)
        return True

    def resolve_dawn(self, now: float | None = None) -> None:
        g = self.g
        dead = sorted({pid for pid in (g.night_kill, g.night_poison) if pid is not None and g.player(pid).alive})
        for pid in dead:
            self.kill_player(pid)
        self.record("death", "天亮了，" + ("昨夜死亡：" + "、".join(f"{pid}号" for pid in dead) if dead else "昨夜是平安夜") + "。")
        if self.check_win(now):
            return
        if dead:
            g.last_words_queue = dead[1:]
            g.after_last_words = "day_speech"
            self.enter("last_words", dead[0], now)
        else:
            self.begin_day(now)

    def resolve_vote(self, now: float | None = None) -> None:
        g = self.g
        tally = Counter(v for v in g.votes.values() if v is not None)
        self.record("vote", "投票结果：" + "；".join(f"{pid}→{target if target is not None else '弃票'}" for pid, target in sorted(g.votes.items())) + "。", votes=dict(g.votes))
        self.emit("vote_result", {"votes": dict(g.votes)})
        top = [pid for pid, count in tally.items() if count == max(tally.values())] if tally else []
        if len(top) == 1:
            pid = top[0]
            self.kill_player(pid)
            self.record("death", f"{pid}号被放逐。")
            if not self.check_win(now):
                g.last_words_queue = []
                g.after_last_words = "night_discussion"
                self.enter("last_words", pid, now)
        else:
            self.record("system", "平票或全部弃票，本轮无人被放逐。")
            self.next_night(now)

    def next_night(self, now: float | None = None) -> None:
        g = self.g
        g.day += 1
        if g.day > 40:
            self.finish("draw", now)
            return
        g.night_kill = g.night_poison = None
        g.night_choices = {}
        self.record("system", f"第 {g.day} 夜开始。")
        self.enter("night_discussion", now=now)

    def check_win(self, now: float | None = None) -> bool:
        wolves = sum(p.role == "wolf" for p in self.g.alive_players())
        good = len(self.g.alive_players()) - wolves
        if wolves == 0 or wolves >= good:
            self.finish("good" if wolves == 0 else "wolves", now)
            return True
        return False

    def finish(self, winner: str, now: float | None = None) -> None:
        self.g.game_over = True
        self.g.winner = winner
        self.g.lifecycle = "FINISHED"
        self.g.finished_at = time.time() if now is None else now
        self.enter("finished", now=now)
        self.record("game_over", {"good": "好人阵营获胜。", "wolves": "狼人阵营获胜。", "draw": "达到 40 天上限，本局平局。"}[winner])
        self.emit("game_finished", {"winner": winner})

    def configure_pet(self, owner_id: str, changes: dict[str, Any]) -> None:
        pet = self.g.pets[owner_id]
        if changes.get("personality", pet.personality) not in PERSONALITIES or changes.get("provider", pet.provider) not in PROVIDERS:
            raise ValueError("未知模型或性格")
        if changes.get("control_mode", pet.control_mode) not in {"copilot", "consult", "autopilot"}:
            raise ValueError("未知宠物模式")
        if "delegate_next" in changes and type(changes["delegate_next"]) is not bool:
            raise ValueError("代打设置必须为布尔值")
        style = changes.get("play_style", {})
        if not isinstance(style, dict) or any(k not in {"aggression", "caution", "logic", "deception", "length", "social"} for k in style):
            raise ValueError("未知风格参数")
        for key, val in style.items():
            if key == "social":
                if not isinstance(val, str) or len(val) > 30: raise ValueError("社交风格最多 30 字")
            elif key == "length":
                if type(val) is not int or not 40 <= val <= 180: raise ValueError("发言长度范围 40 到 180")
            elif type(val) not in {int, float} or not 0 <= val <= 1:
                raise ValueError("风格参数范围 0 到 1")
        # Validate the whole command before changing either control or style.
        for key in ("name", "personality", "provider", "control_mode", "model_key"):
            if key in changes:
                setattr(pet, key, str(changes[key])[:180] if key == "model_key" else str(changes[key])[:24])
        if "provider" in changes and "model_key" not in changes:
            pet.model_key = ""
        if "delegate_next" in changes:
            pet.delegate_next = changes["delegate_next"]
        if changes.get("control_mode") in {"copilot", "consult"}:
            pet.delegate_next = False
        pet.play_style.update(style)
        self.emit("private_pet_message", {"configured": True}, "player", self.g.owned_player(owner_id).id)

    def pet_message(self, owner_id: str, role: str, text: str, client_message_id: str | None = None) -> None:
        pet = self.g.pets[owner_id]
        entry = self.emit("private_pet_message", {"role": role, "text": text[:2000], "time": time.time(), "client_message_id": client_message_id}, "player", self.g.owned_player(owner_id).id)
        pet.private_chat_history.append(entry)
        pet.private_chat_history = pet.private_chat_history[-120:]

    def consume_delegate(self, owner_id: str) -> None:
        self.g.pets[owner_id].delegate_next = False
        self.emit("pet_control_changed", {}, "player", self.g.owned_player(owner_id).id)

    def remember(self, pid: int, memory: dict[str, Any]) -> None:
        self.g.player(pid).memory = deepcopy(memory)
        self.g.state_revision += 1

    def pet_memory(self, owner_id: str, memory: dict[str, Any]) -> None:
        self.g.pets[owner_id].current_game_memory = deepcopy(memory)
        self.g.state_revision += 1

    def kill_player(self, pid: int) -> None:
        p = self.g.player(pid)
        p.wolf_visible_until = self.g.event_seq
        p.wolf_teammates_at_death = [{"id": q.id, "alive": q.alive} for q in self.g.players if q.role == "wolf" and q.id != pid]
        p.alive = False
        self.g.state_revision += 1

    def leave(self, owner_id: str) -> None:
        if self.g.phase != "lobby":
            raise ValueError("开局后不能退座，请关闭页面或切换托管")
        if owner_id == self.g.host_id:
            raise ValueError("房主请使用关闭房间")
        p = self.g.owned_player(owner_id)
        self.g.players.remove(p)
        self.g.pets.pop(owner_id, None)
        self.record("system", f"{p.name} 离开了房间。")

    def moderate(self, owner_id: str, kind: str, payload: dict[str, Any]) -> None:
        if owner_id != self.g.host_id:
            raise PermissionError("只有房主可以管理房间")
        if self.g.lifecycle != "LOBBY":
            raise ValueError("房间管理仅在等待入座时可用")
        if kind == "lock":
            self.g.locked = payload["locked"]
        elif kind == "password":
            from .security import hash_password
            self.g.password_hash = hash_password(payload.get("password", ""))
        elif kind == "kick":
            player = next((p for p in self.g.players if p.id == payload["seat"]), None)
            if not player or not player.owner_id or player.owner_id == self.g.host_id:
                raise ValueError("不能移除该座位")
            self.g.blocked_owners[player.owner_id] = player.id
            if len(self.g.blocked_owners) > 100:
                self.g.blocked_owners.pop(next(iter(self.g.blocked_owners)))
            self.g.players.remove(player)
            self.g.pets.pop(player.owner_id, None)
            self.record("system", f"{player.id}号座位已由房主移除。")
        elif kind == "reopen":
            self.g.blocked_owners = {key: seat for key, seat in self.g.blocked_owners.items() if seat != payload["seat"]}
        self.emit("room_moderated", {"locked": self.g.locked, "has_password": bool(self.g.password_hash)})

    def close_room(self, owner_id: str) -> None:
        if self.g.host_id != owner_id:
            raise ValueError("只有房主可以关闭房间")
        self.g.lifecycle = "ARCHIVED"
        self.g.archived_at = time.time()
        self.g.turn_deadline = None
        self.emit("room_closed", {"lifecycle": "ARCHIVED"})

    def rematch(self, owner_id: str) -> None:
        if self.g.host_id != owner_id or not self.g.game_over:
            raise ValueError("只有房主可以在结束后再来一局")
        old = self.g
        humans = [(p.id, p.name, p.owner_id) for p in old.players if p.owner_id]
        presets = {str(p.id): {"id": p.id, "provider": p.provider, "personality": p.personality, "model_key": p.model_key, "model_locked": p.model_locked} for p in old.players if not p.owner_id}
        fresh = WerewolfGame(old.room_id, old.host_id, title=old.title, pace=old.pace,
                             seat_presets=presets, unique_model_per_ai_seat=old.unique_model_per_ai_seat,
                             creation_action_id=old.creation_action_id, locked=old.locked,
                             password_hash=old.password_hash, blocked_owners=deepcopy(old.blocked_owners))
        old.__dict__.clear()
        old.__dict__.update(fresh.__dict__)
        self.outbox.clear()
        for seat, name, identity in humans:
            self.join(identity, name, seat)
        self.emit("rematch_created", {})

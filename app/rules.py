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
from .roles import GAME_MODES, ROLE_DEFINITIONS, NIGHT_SKILLS, validate_mode

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

    def join(self, owner_id: str, name: str, seat: int | None = None) -> Player:
        g = self.g
        if g.phase != "lobby":
            raise ValueError("游戏已经开始，暂不能加入")
        if g.owned_player(owner_id):
            raise ValueError("你已经在房间中")
        if seat is None:
            available = [seat for seat in range(1, g.player_count+1) if not any(p.id == seat and p.owner_id for p in g.players)]
            if not available:
                raise ValueError("房间已满")
            seat = random.SystemRandom().choice(available)
        existing = next((p for p in g.players if p.id == seat), None)
        if type(seat) is not int or not 1 <= seat <= g.player_count or (existing is not None and existing.owner_id):
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

    def configure(self, owner_id: str, pace: str, seats: list[dict[str, Any]], unique_model_per_ai_seat: bool | None = None, *, mode: str | None = None, player_count: int | None = None, roles: list[str] | None = None) -> None:
        if owner_id != self.g.host_id or self.g.phase != "lobby":
            raise ValueError("只有房主可以在开始前配置房间")
        selected_mode = mode or self.g.mode
        selected_roles = roles if roles is not None else (self.g.role_roster if selected_mode == "custom" else None)
        selected_count = player_count if player_count is not None else (self.g.player_count if selected_mode == "custom" else None)
        next_mode, next_count, next_roles = validate_mode(selected_mode, selected_count, selected_roles)
        if any(p.id > next_count for p in self.g.players):
            raise ValueError("人数减少前请先调整超出人数的座位")
        if pace not in TIMINGS:
            raise ValueError("未知速度")
        if unique_model_per_ai_seat is not None and type(unique_model_per_ai_seat) is not bool:
            raise ValueError("独立模型设置必须为布尔值")
        if not isinstance(seats, list) or any(not isinstance(entry, dict) or type(entry.get('id')) is not int for entry in seats):
            raise ValueError("非法座位设置")
        if len({entry["id"] for entry in seats}) != len(seats):
            raise ValueError("座位设置重复")
        for entry in seats:
            if type(entry.get("id")) is not int or not 1 <= entry["id"] <= next_count:
                raise ValueError("非法座位")
            if entry.get("provider", "mock") not in PROVIDERS or entry.get("personality", "detective") not in PERSONALITIES:
                raise ValueError("未知模型或性格")
            existing = next((p for p in self.g.players if p.id == entry["id"]), None)
            if existing and existing.owner_id:
                raise ValueError("真人座位不能配置成 AI")
            if not isinstance(entry.get("model_key", ""), str) or len(entry.get("model_key", "")) > 240:
                raise ValueError("模型标识无效")
            if type(entry.get("model_locked", False)) is not bool:
                raise ValueError("模型锁定设置无效")
            if entry.get("credential_id") is not None and (not isinstance(entry["credential_id"], str) or len(entry["credential_id"]) > 180):
                raise ValueError("凭据标识无效")
            if not isinstance(entry.get("model_id", ""), str) or len(entry.get("model_id", "")) > 200:
                raise ValueError("模型标识无效")
            if not isinstance(entry.get("model_options", {}), dict):
                raise ValueError("模型参数须为对象")
        self.g.pace = pace
        self.g.mode, self.g.player_count, self.g.role_roster = next_mode, next_count, next_roles
        self.g.seat_presets = {key: value for key, value in self.g.seat_presets.items() if int(key) <= next_count}
        if unique_model_per_ai_seat is not None:
            self.g.unique_model_per_ai_seat = unique_model_per_ai_seat
        for entry in seats:
            self.g.seat_presets[str(entry["id"])] = {"id": entry["id"], "provider": entry.get("provider", "mock"), "personality": entry.get("personality", "detective"), "model_key": entry.get("model_key", ""), "model_locked": entry.get("model_locked", False), "credential_id": entry.get("credential_id"), "credential_owner_id": entry.get("credential_owner_id"), "model_id": entry.get("model_id", ""), "model_options": deepcopy(entry.get("model_options", {}))}
        self.emit("lobby_configured", {})

    def start(self, owner_id: str, now: float | None = None, model_assignments: dict[int, str] | None = None) -> None:
        if owner_id != self.g.host_id or self.g.phase != "lobby":
            raise ValueError("只有房主可以开始未开局的房间")
        for seat in range(1, self.g.player_count+1):
            if not any(p.id == seat for p in self.g.players):
                preset = self.g.seat_presets.get(str(seat), {})
                key = (model_assignments or {}).get(seat) or preset.get("model_key") or "mock:mock"
                provider, model = key.split(":", 1)
                self.g.players.append(Player(seat, f"AI {seat}号", provider=provider, model=model, model_key=key,
                    model_locked=preset.get("model_locked", False), credential_id=preset.get("credential_id"), credential_owner_id=preset.get("credential_owner_id"), model_id=preset.get("model_id", ""), model_options=deepcopy(preset.get("model_options", {})), personality=preset.get("personality", list(PERSONALITIES)[(seat-1) % len(PERSONALITIES)]),
                    voice_profile={"voice_id": "", "rate": 0.9 + seat*0.035, "pitch": 0.8+seat*0.08, "volume": 0.8}))
        self.g.players.sort(key=lambda p: p.id)
        roles = list(self.g.role_roster)
        random.SystemRandom().shuffle(roles)
        for p, role in zip(self.g.players, roles):
            p.role = role
            p.role_state = {"antidote": True, "poison": True} if role == "witch" else {}
        self.g.lifecycle = "ACTIVE"
        self.record("system", "身份牌已私下发放。天黑请闭眼。")
        self.enter("night_discussion", now=now)

    def change_seat(self, owner_id: str, seat: int | None = None) -> Player:
        g = self.g
        if g.phase != 'lobby':
            raise ValueError('只能在大厅换座')
        p = g.owned_player(owner_id)
        if not p:
            raise ValueError('你不属于此房间')
        choices = [i for i in range(1, g.player_count+1) if not any(q.id == i and q.owner_id for q in g.players)]
        if seat is None:
            if not choices:
                raise ValueError('没有可换的空座')
            seat = random.SystemRandom().choice(choices)
        if type(seat) is not int or not 1 <= seat <= g.player_count or any(q.id == seat and q.owner_id and q != p for q in g.players):
            raise ValueError('座位不可用')
        if seat == p.id:
            return p
        old = p.id
        g.players = [q for q in g.players if q.id != seat or q.owner_id]
        p.id = seat
        g.players.sort(key=lambda q: q.id)
        self.record('system', f'{p.name} 从 {old} 号换到 {seat} 号座位。')
        return p

    def required_actors(self, phase: str | None = None) -> list[int]:
        g = self.g
        phase = phase or g.phase
        if phase in {'night_discussion', 'night_wolves'}:
            return [p.id for p in g.wolf_actors()]
        if phase in NIGHT_SKILLS:
            role, action, _, _ = NIGHT_SKILLS[phase]
            players = [p for p in g.alive_players() if p.role == role]
            if action == 'witch':
                players = [p for p in players if p.role_state.get('antidote', g.witch_antidote) or p.role_state.get('poison', g.witch_poison)]
            return [p.id for p in players]
        if phase == 'day_vote':
            return [p.id for p in g.alive_players() if not p.role_state.get('vote_disabled')]
        if phase in {'day_speech', 'last_words', 'death_skill'}:
            return [g.current_turn_player_id] if g.current_turn_player_id is not None else []
        return []

    def enter(self, phase: str, pid: int | None = None, now: float | None = None) -> None:
        g = self.g
        g.phase = phase
        g.current_turn_player_id = pid
        g.turn_sequence += 1
        g.turn_started_at = time.time() if now is None else now
        g.turn_duration = TIMINGS[g.pace].get(phase, 0) * self.time_scale
        g.turn_deadline = g.turn_started_at + g.turn_duration if g.turn_duration else None
        g.submitted = []
        g.current_speech = ''
        if phase == 'day_vote':
            g.votes = {}
        self.emit('phase_changed', {'phase': phase, 'phase_name': PHASE_NAMES[phase],
                  'current_turn_player_id': pid if phase in {'day_speech', 'last_words'} else None,
                  'turn_deadline': g.turn_deadline, 'turn_duration': g.turn_duration,
                  'deadline_semantics': 'maximum_wait'})
        self.emit('vote_started' if phase == 'day_vote' else 'turn_started')
        if phase in self.night_phases and not self.required_actors():
            self.advance_phase(now)
        elif phase == 'day_vote' and not self.required_actors():
            self.resolve_vote(now)

    @property
    def night_phases(self) -> tuple[str, ...]:
        # The role catalogue owns night scheduling, including extra wolf skills.
        phases = [(ROLE_DEFINITIONS[role].night_order or 30, phase) for phase, (role, _, _, _) in NIGHT_SKILLS.items()]
        # Wolf beauty participates in the communal kill at 20 and charms at 30.
        phases = [(30 if phase == 'night_beauty' else order, phase) for order, phase in phases]
        return ('night_discussion',) + tuple(phase for _, phase in sorted(phases + [(20, 'night_wolves')]))

    def _action(self, p: Player, action: str, options: list[int], **extra: Any) -> dict[str, Any]:
        g = self.g
        definition = ROLE_DEFINITIONS[p.role]
        return {'type': action, 'label': {
            'wolf_discuss': '完成狼队讨论', 'wolf_kill': '选择夜杀目标', 'seer_inspect': '查验',
            'witch': '女巫用药', 'guard_protect': '守护', 'wolf_beauty_charm': '魅惑',
            'hunter_shoot': '猎人开枪', 'wolf_king_shoot': '狼王开枪', 'duel': '骑士决斗',
            'self_destruct': '白狼王自爆', 'speech': '结束发言', 'vote': '放逐投票'}.get(action, action),
            'options': options, 'turn_sequence': g.turn_sequence, 'game_id': g.game_id,
            'turn_id': g.turn_id, 'state_revision': g.state_revision,
            'rules': definition.rules, **extra}

    def action_for(self, pid: int) -> dict[str, Any] | None:
        g, p = self.g, self.g.player(pid)
        exceptional = g.phase in {'last_words', 'death_skill'} and pid == g.current_turn_player_id
        if g.lifecycle != 'ACTIVE' or g.game_over or (not p.alive and not exceptional) or pid in g.submitted:
            return None
        targets = [i for i in g.alive_ids() if i != pid]
        if g.phase == 'night_discussion' and pid in self.required_actors():
            return self._action(p, 'wolf_discuss', [], action_schema={'text': 'string (optional)'})
        if g.phase == 'night_wolves' and pid in self.required_actors():
            return self._action(p, 'wolf_kill', ROLE_DEFINITIONS['wolf'].legal_targets(g, p))
        if g.phase in NIGHT_SKILLS and pid in self.required_actors():
            _, action, legal_targets, _ = NIGHT_SKILLS[g.phase]
            extra = {}
            if action == 'witch':
                antidote = p.role_state.get('antidote', g.witch_antidote)
                extra = {'killed': g.night_kill if antidote else None, 'antidote': antidote,
                         'poison': p.role_state.get('poison', g.witch_poison),
                         'can_save': bool(antidote and g.night_kill is not None),
                         'can_use_both': False, 'can_self_save': True,
                         'poison_options': legal_targets(g, p)}
            return self._action(p, action, legal_targets(g, p), **extra)
        if g.phase == 'death_skill' and pid == g.current_turn_player_id:
            return self._action(p, g.death_skill_action, targets)
        if g.phase in {'day_speech', 'last_words'} and pid == g.current_turn_player_id:
            return self._action(p, 'speech', [])
        if g.phase == 'day_vote' and pid in self.required_actors() and str(pid) not in g.votes:
            return self._action(p, 'vote', targets)
        return None

    def secondary_actions(self, pid: int) -> list[dict[str, Any]]:
        g, p = self.g, self.g.player(pid)
        if not p.alive or g.lifecycle != 'ACTIVE' or g.phase != 'day_speech':
            return []
        targets = [q.id for q in g.alive_players() if q.id != pid]
        if p.role == 'knight' and not p.role_state.get('duel_used'):
            return [self._action(p, 'duel', targets, optional=True, requires_target=True)]
        if p.role == 'white_wolf_king' and not p.role_state.get('self_destruct_used'):
            return [self._action(p, 'self_destruct', targets, optional=True, requires_target=True)]
        return []

    def validate(self, pid: int, payload: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        g = self.g
        now = time.time() if now is None else now
        if payload.get('game_id') != g.game_id or payload.get('turn_sequence') != g.turn_sequence:
            raise ValueError('回合已变更，请刷新当前状态')
        if payload.get('turn_id') is not None and payload['turn_id'] != g.turn_id:
            raise ValueError('回合已结束，请同步状态')
        if payload.get('expected_state_revision') is not None and payload['expected_state_revision'] != g.state_revision:
            raise ValueError('状态版本已变更，请同步后重试')
        if g.turn_deadline is None or now >= g.turn_deadline:
            raise ValueError('回合已超时')
        pending = next((action for action in self.secondary_actions(pid) if action['type'] == payload.get('action')), None)
        pending = pending or self.action_for(pid)
        if not pending or payload.get('action') != pending['type']:
            raise ValueError('当前不能执行此动作或已提交')
        def target_valid(value: Any) -> bool:
            return value is None or (type(value) is int and value in pending['options'])
        target_actions = {'vote', 'wolf_kill', 'seer_inspect', 'guard_protect', 'wolf_beauty_charm', 'hunter_shoot', 'wolf_king_shoot', 'duel', 'self_destruct'}
        if pending['type'] in target_actions and not target_valid(payload.get('target')):
            raise ValueError('目标不合法')
        if pending.get('requires_target') and payload.get('target') is None:
            raise ValueError('技能须指定存活目标')
        if pending['type'] == 'speech' and (not isinstance(payload.get('speech', ''), str) or len(payload.get('speech', '')) > 500):
            raise ValueError('发言最多 500 字')
        if pending['type'] == 'wolf_discuss' and (not isinstance(payload.get('text', ''), str) or len(payload.get('text', '')) > 500):
            raise ValueError('狼队讨论最多 500 字')
        if pending['type'] == 'witch':
            if type(payload.get('save', False)) is not bool:
                raise ValueError('解药选择无效')
            if payload.get('save') and (not pending['antidote'] or g.night_kill is None):
                raise ValueError('解药不可用')
            if not target_valid(payload.get('poison_target')) or (payload.get('poison_target') is not None and not pending['poison']):
                raise ValueError('毒药不可用或目标不合法')
            if payload.get('save') and payload.get('poison_target') is not None:
                raise ValueError('同夜只能使用一种药')
        return pending

    def apply(self, pid: int, payload: dict[str, Any], now: float | None = None) -> bool:
        pending = self.validate(pid, payload, now)
        g, p = self.g, self.g.player(pid)
        notes_before, old_sequence = len(p.private_notes), g.turn_sequence
        action, target = pending['type'], payload.get('target')
        g.action_history.append({'day': g.day, 'phase': g.phase, 'player_id': pid, 'action': action,
                                 'target': target, 'save': payload.get('save', False),
                                 'poison_target': payload.get('poison_target'),
                                 **({'speech': payload.get('speech', '')} if action == 'speech' else {})})
        if action in {'duel', 'self_destruct'}:
            self.day_skill(p, action, target, now)
            return g.turn_sequence != old_sequence
        if action == 'wolf_discuss':
            if payload.get('text', '').strip():
                self.wolf_message(pid, payload['text'].strip())
        elif action == 'wolf_kill':
            ROLE_DEFINITIONS['wolf'].apply_action(self, p, payload)
        elif g.phase in NIGHT_SKILLS:
            NIGHT_SKILLS[g.phase][3](self, p, payload)
        elif action in {'hunter_shoot', 'wolf_king_shoot'}:
            from .roles import shoot_action
            shoot_action(self, p, payload)
        elif action == 'speech':
            speech = payload.get('speech', '').strip() or g.current_speech or '（结束发言）'
            self.record('speech', f'{pid}号 {p.name}：{speech}', pid, speech=speech)
            self.emit('speech_finished', {'player_id': pid, 'speech': speech, 'content': speech})
            g.submitted.append(pid)
            self.emit('turn_finished', {'player_id': pid})
            self.advance_speech(now)
            return g.turn_sequence != old_sequence
        elif action == 'vote':
            g.votes[str(pid)] = target
            self.emit('vote_submitted', {'player_id': pid})
        g.submitted.append(pid)
        self.emit('player_action', {'action': action, 'target': target,
                  'save': payload.get('save', False), 'poison_target': payload.get('poison_target'),
                  'private_notes': p.private_notes[notes_before:]}, 'player', pid)
        if g.phase == 'death_skill':
            self.continue_deaths(now)
        elif all(actor in g.submitted for actor in self.required_actors()):
            if g.phase == 'day_vote':
                self.resolve_vote(now)
            else:
                self.advance_phase(now)
        return g.turn_sequence != old_sequence

    def advance_phase(self, now: float | None = None) -> None:
        g = self.g
        if g.phase == 'night_discussion':
            g.night_choices = {}
        if g.phase == 'night_wolves':
            votes = Counter(target for target in g.night_choices.values() if target is not None)
            majority = len(g.wolf_actors()) // 2 + 1
            g.night_kill = next((target for target, count in votes.items() if count >= majority), None)
        phases = self.night_phases
        if g.phase in phases and phases.index(g.phase)+1 < len(phases):
            self.enter(phases[phases.index(g.phase)+1], now=now)
        else:
            self.resolve_dawn(now)

    def append_speech(self, pid: int, chunk: str, sequence: int, now: float | None = None) -> bool:
        g = self.g
        if g.turn_sequence != sequence or not self.action_for(pid) or g.phase not in {'day_speech', 'last_words'}:
            return False
        if (time.time() if now is None else now) >= (g.turn_deadline or 0):
            return False
        chunk = chunk[:max(0, 500-len(g.current_speech))]
        if not g.current_speech:
            self.emit('speech_started', {'player_id': pid})
        g.current_speech += chunk
        self.emit('speech_chunk', {'player_id': pid, 'chunk': chunk, 'delta': chunk})
        return True

    def advance_speech(self, now: float | None = None) -> None:
        g = self.g
        if g.phase == 'last_words':
            if g.last_words_queue:
                self.enter('last_words', g.last_words_queue.pop(0), now)
            elif g.after_last_words == 'day_speech':
                self.begin_day(now)
            elif g.after_last_words == 'resume':
                self.resume_day(now)
            else:
                self.next_night(now)
        else:
            g.speech_queue = [pid for pid in g.speech_queue if g.player(pid).alive]
            if g.speech_queue:
                self.enter('day_speech', g.speech_queue.pop(0), now)
            else:
                self.record('system', '所有玩家发言完毕，开始投票。提交后仅显示已投状态，结束时公布票型。')
                self.enter('day_vote', now=now)

    def begin_day(self, now: float | None = None) -> None:
        if self.check_win(now):
            return
        self.g.speech_queue = self.g.alive_ids()
        self.enter('day_speech', self.g.speech_queue.pop(0), now)

    def wolf_message(self, pid: int, text: str) -> None:
        p = self.g.player(pid)
        if not p.alive or not self.g.in_wolf_channel(p) or self.g.phase not in {'night_discussion', 'night_wolves'}:
            raise ValueError('只有存活狼人可以在夜间使用狼队频道')
        if not text.strip() or len(text) > 500:
            raise ValueError('消息需为 1 到 500 字')
        entry = self.emit('wolf_chat_message', {'player_id': pid, 'day': self.g.day, 'text': text, 'seq': len(self.g.wolf_chat)+1}, 'wolves')
        self.g.wolf_chat.append(entry)

    def tick(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        g = self.g
        if g.turn_deadline is None or now < g.turn_deadline or g.game_over:
            return False
        if g.phase in self.night_phases:
            # Only missing actors time out; legal submissions retain their effects.
            for pid in self.required_actors():
                if pid not in g.submitted:
                    pending_type = self.action_for(pid)['type']
                    g.submitted.append(pid)
                    if g.phase == 'night_wolves':
                        g.night_choices[str(pid)] = None
                    elif g.phase == 'night_guard':
                        g.player(pid).role_state['last_guard_target'] = None
                    elif g.phase == 'night_beauty':
                        g.player(pid).role_state.update(charmed_target=None, last_charm_target=None)
                    g.action_history.append({'day': g.day, 'phase': g.phase, 'player_id': pid,
                                             'action': pending_type,
                                             'target': None, 'timed_out': True})
                    self.emit('action_timeout', {'action': g.phase}, 'player', pid)
            self.advance_phase(now)
        elif g.phase == 'death_skill':
            g.player(g.current_turn_player_id).role_state['shot_used'] = True
            self.emit('action_timeout', {'action': g.death_skill_action}, 'player', g.current_turn_player_id)
            self.continue_deaths(now)
        elif g.phase in {'day_speech', 'last_words'}:
            pid = g.current_turn_player_id
            self.record('speech', f'{pid}号：{g.current_speech or "（发言超时）"}', pid, speech=g.current_speech or '（发言超时）')
            self.emit('speech_finished', {'player_id': pid, 'speech': g.current_speech, 'content': g.current_speech or '（发言超时）', 'partial': True})
            self.emit('turn_finished', {'player_id': pid, 'timeout': True})
            self.advance_speech(now)
        elif g.phase == 'day_vote':
            for pid in self.required_actors():
                g.votes.setdefault(str(pid), None)
            self.resolve_vote(now)
        return True

    def resolve_dawn(self, now: float | None = None) -> None:
        g = self.g
        guards = set(g.night_guards.values())
        poisoned = set(g.night_poisons + ([g.night_poison] if g.night_poison is not None else []))
        knife = g.night_kill if g.night_kill not in guards else None
        dead = sorted({pid for pid in ([knife] + list(poisoned)) if pid is not None and g.player(pid).alive})
        g.death_context = {'after': 'day_speech', 'dead': []}
        for pid in dead:
            self.kill_player(pid, cause='poison' if pid in poisoned else 'knife')
        deaths = sorted(g.death_context['dead'])
        self.record('death', '天亮了，' + ('昨夜死亡：' + '、'.join(f'{pid}号' for pid in deaths) if deaths else '昨夜是平安夜') + '。')
        self.continue_deaths(now)

    def resolve_vote(self, now: float | None = None) -> None:
        g = self.g
        tally = Counter(target for target in g.votes.values() if target is not None)
        self.record('vote', '投票结果：' + '；'.join(f'{pid}→{target if target is not None else "弃票"}' for pid, target in sorted(g.votes.items(), key=lambda pair: int(pair[0]))) + '。', votes=dict(g.votes))
        self.emit('vote_result', {'votes': dict(g.votes)})
        top = [pid for pid, count in tally.items() if count == max(tally.values())] if tally else []
        if len(top) == 1:
            pid, p = top[0], g.player(top[0])
            if p.role == 'idiot' and not p.role_state.get('revealed'):
                p.role_state.update(revealed=True, vote_disabled=True)
                self.record('skill', f'{pid}号翻开白痴身份牌，免于本次放逐，此后失去投票权。', pid, revealed_role='idiot')
                self.next_night(now)
                return
            g.death_context = {'after': 'next_night', 'dead': []}
            self.kill_player(pid, cause='vote')
            self.record('death', f'{pid}号被放逐。')
            self.continue_deaths(now)
        else:
            self.record('system', '平票或全部弃票，本轮无人被放逐。')
            self.next_night(now)

    def day_skill(self, p: Player, action: str, target: int, now: float | None = None) -> None:
        g = self.g
        g.resume_phase, g.resume_player_id = g.phase, g.current_turn_player_id
        g.death_context = {'after': 'resume' if action == 'duel' else 'next_night', 'dead': []}
        if action == 'duel':
            p.role_state['duel_used'] = True
            p.role_state['revealed'] = True
            victim = target if ROLE_DEFINITIONS[g.player(target).role].faction == 'wolves' else p.id
            self.record('skill', f'{p.id}号骑士向 {target} 号发起决斗，{victim}号死亡。', p.id, action=action, target=target, revealed_role='knight')
            self.kill_player(victim, cause='duel')
        else:
            p.role_state['self_destruct_used'] = True
            p.role_state['revealed'] = True
            self.record('skill', f'{p.id}号白狼王自爆，带走 {target} 号，白天结束。', p.id, action=action, target=target, revealed_role='white_wolf_king')
            self.kill_player(p.id, cause='self_destruct')
            self.kill_player(target, cause='self_destruct')
        self.continue_deaths(now)

    def continue_deaths(self, now: float | None = None) -> None:
        g = self.g
        if g.death_skill_queue:
            skill = g.death_skill_queue.pop(0)
            g.death_skill_action = skill['action']
            self.enter('death_skill', skill['player_id'], now)
            return
        g.death_skill_action = None
        if self.check_win(now):
            return
        context = g.death_context or {'after': 'day_speech', 'dead': []}
        g.death_context = {}
        dead = context['dead']
        g.after_last_words = context['after']
        if dead:
            g.last_words_queue = dead[1:]
            self.enter('last_words', dead[0], now)
        elif context['after'] == 'day_speech':
            self.begin_day(now)
        elif context['after'] == 'resume':
            self.resume_day(now)
        else:
            self.next_night(now)

    def resume_day(self, now: float | None = None) -> None:
        g = self.g
        pid = g.resume_player_id
        g.resume_player_id = None
        g.speech_queue = [i for i in g.speech_queue if g.player(i).alive]
        if pid is not None and g.player(pid).alive:
            self.enter('day_speech', pid, now)
        else:
            # Use the normal speech continuation without replaying dead actors.
            g.phase = 'day_speech'
            self.advance_speech(now)

    def next_night(self, now: float | None = None) -> None:
        g = self.g
        if self.check_win(now):
            return
        g.day += 1
        if g.day > 40:
            self.finish('draw', now)
            return
        g.night_kill = g.night_poison = None
        g.night_choices, g.night_guards = {}, {}
        g.night_saved, g.night_poisons = [], []
        self.record('system', f'第 {g.day} 夜开始。')
        self.enter('night_discussion', now=now)

    def check_win(self, now: float | None = None) -> bool:
        if self.g.game_over:
            return True
        if self.g.death_skill_queue or (self.g.phase == 'death_skill' and self.g.death_skill_action):
            return False
        wolves = sum(ROLE_DEFINITIONS[p.role].faction == 'wolves' for p in self.g.alive_players())
        good = len(self.g.alive_players()) - wolves
        if wolves == 0 or wolves >= good:
            self.finish('good' if wolves == 0 else 'wolves', now)
            return True
        return False

    def finish(self, winner: str, now: float | None = None) -> None:
        self.g.game_over = True
        self.g.winner = winner
        self.g.lifecycle = 'FINISHED'
        self.g.finished_at = time.time() if now is None else now
        self.enter('finished', now=now)
        self.record('game_over', {'good': '好人阵营获胜。', 'wolves': '狼人阵营获胜。', 'draw': '达到 40 天上限，本局平局。'}[winner])
        self.emit('game_finished', {'winner': winner})

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
                setattr(pet, key, str(changes[key])[:240] if key == "model_key" else str(changes[key])[:24])
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

    def kill_player(self, pid: int, cause: str = "knife") -> None:
        p = self.g.player(pid)
        if not p.alive:
            return
        p.wolf_visible_until = self.g.event_seq
        p.wolf_teammates_at_death = [{"id": q.id, "alive": q.alive} for q in self.g.players
                                   if self.g.in_wolf_channel(q) and q.id != pid]
        p.alive = False
        p.role_state["death_cause"] = cause
        self.g.state_revision += 1
        if self.g.death_context and pid not in self.g.death_context["dead"]:
            self.g.death_context["dead"].append(pid)
        trigger = ROLE_DEFINITIONS[p.role].death_trigger
        if trigger:
            trigger(self, p, cause)
        if not any(ROLE_DEFINITIONS[q.role].participates_in_kill for q in self.g.alive_players()):
            for q in self.g.alive_players():
                if q.role == 'hidden_wolf' and 'wolf_access_start' not in q.role_state:
                    q.role_state['wolf_access_start'] = self.g.event_seq+1
                    q.private_notes.append('普通狼人全部死亡，你已觉醒，可以使用狼队频道并参与夜杀。')

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
        presets = {str(p.id): {"id": p.id, "provider": p.provider, "personality": p.personality, "model_key": p.model_key, "model_locked": p.model_locked, "credential_id": p.credential_id, "credential_owner_id": p.credential_owner_id, "model_id": p.model_id, "model_options": deepcopy(p.model_options)} for p in old.players if not p.owner_id}
        fresh = WerewolfGame(old.room_id, old.host_id, title=old.title, pace=old.pace,
                             seat_presets=presets, unique_model_per_ai_seat=old.unique_model_per_ai_seat,
                             mode=old.mode, player_count=old.player_count, role_roster=list(old.role_roster),
                             creation_action_id=old.creation_action_id, locked=old.locked,
                             password_hash=old.password_hash, blocked_owners=deepcopy(old.blocked_owners))
        old.__dict__.clear()
        old.__dict__.update(fresh.__dict__)
        self.outbox.clear()
        for seat, name, identity in humans:
            self.join(identity, name, seat)
        self.emit("rematch_created", {})

"""The ONLY boundary used for client, AI player, and private pet contexts."""

from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from .boards import framework
from .game import PHASE_NAMES, ROLE_NAMES, WerewolfGame
from .roles import GAME_MODES, ROLE_DEFINITIONS
from .rules import RuleEngine


class InformationScope:
    @staticmethod
    def player_view(g: WerewolfGame, pid: int) -> dict[str, Any]:
        p = g.player(pid)
        started = g.phase != "lobby"
        random_board = g.board_policy != "fixed" and not g.game_over
        visible_roster = list(g.random_role_pool) if random_board else list(g.role_roster)
        # Night actor IDs would disclose who holds a hidden role.
        public_turn = g.current_turn_player_id if g.phase in {"day_speech", "last_words"} else None
        presets = {
            key: {k: deepcopy(v) for k, v in preset.items() if k not in {"credential_id", "credential_owner_id"}}
            for key, preset in g.seat_presets.items()
        }
        for key, preset in g.seat_presets.items():
            presets[key]["has_credential"] = bool(preset.get("credential_id"))
        view = {
            "room_id": g.room_id,
            "game_id": g.game_id,
            "title": g.title,
            "pace": g.pace,
            "state_revision": g.state_revision,
            "turn_id": g.turn_id,
            "event_seq": g.event_seq,
            "lifecycle": g.lifecycle,
            "suspended_remaining": g.suspended_remaining,
            "seat_presets": presets,
            "mode": g.mode,
            "player_count": g.player_count,
            "board_policy": g.board_policy,
            "random_role_pool": list(g.random_role_pool),
            "board_framework": framework(g.player_count) if random_board else None,
            "role_roster": [] if random_board else list(g.role_roster),
            "game_mode": GAME_MODES[g.mode].public()
            if g.mode in GAME_MODES
            else {
                "key": "custom",
                "display_name": "约束随机板" if random_board else "自定义板子",
                "player_count": g.player_count,
                "roles": [] if random_board else list(g.role_roster),
            },
            "rules": {
                "roles": [ROLE_DEFINITIONS[role].public() for role in dict.fromkeys(visible_roster)],
                "victory": "狼阵营全部死亡好人胜；狼人数达到或超过好人数狼人胜。",
                "voting": "可弃票、不可投自己；最高票平票无人出局。翻牌白痴失去投票权。",
                "timing": "所有动作完成即推进，倒计时仅为最长等待时间。",
            },
            "locked": g.locked,
            "has_password": bool(g.password_hash),
            "unique_model_per_ai_seat": g.unique_model_per_ai_seat,
            "day": g.day,
            "phase": "night" if random_board and g.phase.startswith("night_") else g.phase,
            "phase_name": "夜间行动" if random_board and g.phase.startswith("night_") else PHASE_NAMES[g.phase],
            "game_over": g.game_over,
            "winner": g.winner,
            "current_turn_player_id": public_turn,
            "turn_started_at": g.turn_started_at,
            "turn_deadline": g.turn_deadline,
            "turn_duration": g.turn_duration,
            "turn_sequence": g.turn_sequence,
            "server_time": time.time(),
            "current_speech": g.current_speech,
            "players": [
                {
                    "id": q.id,
                    "name": q.name,
                    "alive": q.alive,
                    "is_human": bool(q.owner_id),
                    "is_you": q.id == pid,
                    "provider": q.provider if not q.owner_id else "human",
                    "personality": q.personality,
                    "agent_id": q.agent_id if not q.owner_id else None,
                    "model": q.model if not q.owner_id else None,
                    "model_key": q.model_key if not q.owner_id else None,
                    "model_locked": q.model_locked,
                    "voice_profile": deepcopy(q.voice_profile),
                    "execution_status": deepcopy(q.execution_status),
                    "has_credential": bool(q.credential_id),
                    "can_vote": not q.role_state.get("vote_disabled", False),
                    "role": ROLE_NAMES[q.role]
                    if started and (q.id == pid or g.game_over or q.role_state.get("revealed"))
                    else None,
                }
                for q in g.players
            ],
            "self": {
                "id": pid,
                "name": p.name,
                "alive": p.alive,
                "agent_id": p.agent_id,
                "role": ROLE_NAMES[p.role] if started else None,
                "role_key": p.role if started else None,
                "private_notes": list(p.private_notes),
                "memory": deepcopy(p.memory),
                "faction": ROLE_DEFINITIONS[p.role].faction if started else None,
                "wolf_channel": started and g.in_wolf_channel(p),
                "role_state": ROLE_DEFINITIONS[p.role].private_information(g, p) if started else {},
            },
            "events": deepcopy(g.events),
            "vote_status": {str(pid): str(pid) in g.votes for pid in RuleEngine(g).required_actors()}
            if g.phase == "day_vote"
            else {},
            "pending_action": RuleEngine(g).action_for(pid) if started else None,
            "secondary_actions": RuleEngine(g).secondary_actions(pid) if started else [],
            "completion": {"submitted": pid in g.submitted, "deadline_semantics": "maximum_wait"},
        }
        if started and (g.in_wolf_channel(p) or p.role == "hidden_wolf"):
            view["wolf_teammates"] = (
                [
                    {"id": q.id, "alive": q.alive}
                    for q in g.players
                    if ROLE_DEFINITIONS[q.role].wolf_channel and q.id != pid
                ]
                if p.alive
                else deepcopy(p.wolf_teammates_at_death)
            )
            if g.in_wolf_channel(p):
                view["wolf_chat"] = deepcopy(
                    [
                        entry
                        for entry in g.wolf_chat
                        if (p.alive or entry.get("event_seq", entry.get("seq", 0)) <= (p.wolf_visible_until or 0))
                        and entry.get("event_seq", entry.get("seq", 0)) >= p.role_state.get("wolf_access_start", 0)
                    ]
                )
        return view

    @staticmethod
    def owner_view(g: WerewolfGame, owner_id: str) -> dict[str, Any]:
        p = g.owned_player(owner_id)
        if not p:
            raise ValueError("你不属于此房间")
        view = InformationScope.player_view(g, p.id)
        view["is_host"] = g.host_id == owner_id
        if view["is_host"]:
            for key, preset in g.seat_presets.items():
                if preset.get("credential_owner_id") == owner_id:
                    view["seat_presets"][key]["credential_id"] = preset.get("credential_id")
        view["pet"] = asdict(g.pets[owner_id])
        return view

    @staticmethod
    def ai_view(g: WerewolfGame, pid: int) -> dict[str, Any]:
        view = InformationScope.player_view(g, pid)
        # Bound context and costs; the full public history stays available in the UI.
        view["events"] = view["events"][-12:]
        view["self"]["private_notes"] = view["self"]["private_notes"][-20:]
        if "wolf_chat" in view:
            view["wolf_chat"] = view["wolf_chat"][-20:]
        return view

    @staticmethod
    def permits(g: WerewolfGame, owner_id: str, event: dict[str, Any]) -> bool:
        p = g.owned_player(owner_id)
        if not p:
            return False
        return InformationScope.permits_player(g, p.id, event)

    @staticmethod
    def client_event(g: WerewolfGame, event: dict[str, Any]) -> dict[str, Any]:
        packet = deepcopy(event)
        phase = packet.get("data", {}).get("phase", "")
        if g.board_policy != "fixed" and not g.game_over and phase.startswith("night_"):
            packet["data"].update(phase="night", phase_name="夜间行动")
        return packet

    @staticmethod
    def permits_player(g: WerewolfGame, pid: int, event: dict[str, Any]) -> bool:
        p = g.player(pid)
        if event["audience"] == "public":
            return True
        if event["audience"] == "player":
            return event["player_id"] == p.id
        return event["audience"] == "wolves" and g.in_wolf_channel(p) and p.alive

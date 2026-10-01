"""The ONLY boundary used for client, AI player, and private pet contexts."""
from __future__ import annotations

from dataclasses import asdict
from copy import deepcopy
import time
from typing import Any

from .game import PHASE_NAMES, ROLE_NAMES, WerewolfGame
from .rules import RuleEngine


class InformationScope:
    @staticmethod
    def player_view(g: WerewolfGame, pid: int) -> dict[str, Any]:
        p = g.player(pid)
        started = g.phase != "lobby"
        # Night actor IDs would disclose who holds a hidden role.
        public_turn = g.current_turn_player_id if g.phase in {"day_speech", "last_words"} else None
        view = {
            "room_id": g.room_id, "game_id": g.game_id, "title": g.title, "pace": g.pace,
            "state_revision": g.state_revision, "turn_id": g.turn_id, "event_seq": g.event_seq,
            "lifecycle": g.lifecycle, "seat_presets": deepcopy(g.seat_presets),
            "locked": g.locked, "has_password": bool(g.password_hash),
            "unique_model_per_ai_seat": g.unique_model_per_ai_seat,
            "day": g.day, "phase": g.phase, "phase_name": PHASE_NAMES[g.phase],
            "game_over": g.game_over, "winner": g.winner,
            "current_turn_player_id": public_turn,
            "turn_started_at": g.turn_started_at, "turn_deadline": g.turn_deadline,
            "turn_duration": g.turn_duration, "turn_sequence": g.turn_sequence,
            "server_time": time.time(), "current_speech": g.current_speech,
            "players": [{"id": q.id, "name": q.name, "alive": q.alive, "is_human": bool(q.owner_id),
                         "is_you": q.id == pid, "provider": q.provider if not q.owner_id else "human",
                         "personality": q.personality,
                         "agent_id": q.agent_id if not q.owner_id else None,
                         "model": q.model if not q.owner_id else None, "model_key": q.model_key if not q.owner_id else None,
                         "model_locked": q.model_locked, "voice_profile": deepcopy(q.voice_profile),
                         "execution_status": deepcopy(q.execution_status),
                         "role": ROLE_NAMES[q.role] if started and (q.id == pid or g.game_over) else None}
                        for q in g.players],
            "self": {"id": pid, "name": p.name, "alive": p.alive,
                     "agent_id": p.agent_id,
                     "role": ROLE_NAMES[p.role] if started else None,
                     "role_key": p.role if started else None,
                     "private_notes": list(p.private_notes), "memory": deepcopy(p.memory)},
            "events": deepcopy(g.events),
            "vote_status": {str(q.id): str(q.id) in g.votes for q in g.alive_players()} if g.phase == "day_vote" else {},
            "pending_action": RuleEngine(g).action_for(pid) if started else None,
        }
        if started and p.role == "wolf":
            view["wolf_teammates"] = ([{"id": q.id, "alive": q.alive} for q in g.players if q.role == "wolf" and q.id != pid] if p.alive else deepcopy(p.wolf_teammates_at_death))
            view["wolf_chat"] = deepcopy([entry for entry in g.wolf_chat if p.alive or entry.get("event_seq", entry.get("seq", 0)) <= (p.wolf_visible_until or 0)])
        return view

    @staticmethod
    def owner_view(g: WerewolfGame, owner_id: str) -> dict[str, Any]:
        p = g.owned_player(owner_id)
        if not p:
            raise ValueError("你不属于此房间")
        view = InformationScope.player_view(g, p.id)
        view["is_host"] = g.host_id == owner_id
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
    def permits_player(g: WerewolfGame, pid: int, event: dict[str, Any]) -> bool:
        p = g.player(pid)
        if event["audience"] == "public":
            return True
        if event["audience"] == "player":
            return event["player_id"] == p.id
        return event["audience"] == "wolves" and p.role == "wolf" and p.alive

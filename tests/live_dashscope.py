"""Opt-in real API playtest; never runs as part of unittest discovery.
Run only when intentionally authorizing paid live model requests.
"""

import asyncio
import json
import os
import time
from collections import Counter
from copy import deepcopy
from pathlib import Path

import httpx
from dotenv import load_dotenv

from app.ai import AIOrchestrator
from app.game import PERSONALITIES
from app.limits import Limits
from app.llm import LLMRouter
from app.persistence import Store
from app.rooms import RoomManager

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


class ObservedRouter(LLMRouter):
    def __init__(self):
        super().__init__()
        self.actions = []
        self.speeches = []

    async def ask_json(self, provider, system_prompt, user_prompt, **kwargs):
        ctx = kwargs.get("mock_context") or {}
        before = time.monotonic()
        result = await super().ask_json(provider, system_prompt, user_prompt, **kwargs)
        self.actions.append(
            {
                **{key: ctx.get(key) for key in ["room_id", "game_id", "day", "phase"]},
                "actor": ctx.get("player_id"),
                "action": ctx.get("action"),
                "target": result.data.get("target"),
                "provider": result.provider_used,
                "latency": round(time.monotonic() - before, 3),
                "data": deepcopy(result.data),
                "options": ctx.get("options", []),
                "routing": deepcopy(result.routing),
            }
        )
        return result

    async def speech_stream(self, provider, system, user, ctx):
        text = []
        before = time.monotonic()
        async for chunk in super().speech_stream(provider, system, user, ctx):
            text.append(chunk)
            yield chunk
        self.speeches.append(
            {
                **{key: ctx.get(key) for key in ["room_id", "game_id", "day", "phase"]},
                "actor": ctx.get("player_id"),
                "text": "".join(text),
                "latency": round(time.monotonic() - before, 3),
                "routing": self.execution(self._scope.get().get("agent_id")),
            }
        )


class AuditedAI(AIOrchestrator):
    def context(self, view, *args, **kwargs):
        if not view["game_over"]:
            assert all(p["role"] is None for p in view["players"] if p["id"] != view["self"]["id"]), (
                "Hidden role reached AI"
            )
            if view["self"]["role_key"] != "wolf":
                assert "wolf_chat" not in view and "wolf_teammates" not in view, "Wolf context leaked"
        return super().context(view, *args, **kwargs)


class ObservedStore(Store):
    def __init__(self):
        super().__init__(":memory:")
        self.saves = Counter()

    def save(self, game, events=None):
        self.saves[game.room_id] += 1
        super().save(game, events)


async def routing_check():
    router = LLMRouter()
    original = router._dashscope

    async def forced_failure(system, user):
        if router._model("dashscope") == "qwen-plus":
            raise httpx.ReadTimeout("Injected primary failure for acceptance")
        return await original(system, user)

    router._dashscope = forced_failure
    try:
        with router.request_scope(
            room_id="live-failover",
            game_id="acceptance-k1",
            agent_id="independent-probe",
            category="vote",
            guard=Limits(),
        ):
            result = await router.ask_json(
                "dashscope:qwen-plus",
                "只输出合法投票JSON。",
                '只返回 {"target":2}',
                mock_context={"action": "vote", "options": [2], "player_id": 1},
            )
        assert result.provider_used == "dashscope" and result.model_used != "qwen-plus", (
            "Second real model must complete the action"
        )
        assert result.data.get("target") == 2
        return {"injected_primary_timeout": True, "real_backup_completed": True, "routing": result.routing}
    finally:
        await router.close()


async def main():
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise SystemExit("DASHSCOPE_API_KEY is required; no live test was run.")
    # Each of these distinct IDs was probed against the authorized account.
    os.environ.setdefault("DASHSCOPE_MODELS", "qwen-plus,qwen-turbo,qwen-max,qwen-flash,qwen3-max")
    failover = await routing_check()
    router = ObservedRouter()
    store = ObservedStore()
    manager = RoomManager(store, router, ai_pause=1.5)
    manager.ai = AuditedAI(router)
    games = []
    starts = {}
    pet_checks = []
    packet_counts = Counter()
    snapshots = {}
    phases = []

    async def consume(room, owner):
        connection = await manager.subscribe(room.game.room_id, owner)
        try:
            while True:
                packet = await connection.queue.get()
                packet_counts[packet["type"]] += 1
                if packet["type"] == "state_snapshot":
                    snapshots[room.game.room_id] = packet["data"]
        finally:
            room.connections.discard(connection)

    consumers = []
    for index, personality in enumerate(["detective", "cautious", "commander"][: int(os.getenv("LIVE_GAMES", "3"))], 1):
        owner = f"live-owner-{index}"
        created = await manager.create(owner, f"实测玩家{index}", index, f"Qwen 实测 {index}", "fast")
        rid = created["room_id"]
        seats = [
            {"id": pid, "provider": "dashscope", "personality": list(PERSONALITIES)[pid - 1]}
            for pid in range(1, 7)
            if pid != index
        ]
        await manager.command(rid, owner, "configure", {"pace": "fast", "seats": seats})
        await manager.command(
            rid, owner, "pet_config", {"provider": "dashscope", "personality": personality, "control_mode": "autopilot"}
        )
        await manager.command(rid, owner, "start", {})
        room = manager.require(rid, owner)
        assert len({p.model_key for p in room.game.players if not p.owner_id}) == 5, (
            "Strict mode must allocate five distinct real models"
        )
        assert len({p.agent_id for p in room.game.players}) == 6
        games.append((room, owner))
        starts[rid] = time.monotonic()
        consumers.append(asyncio.create_task(consume(room, owner)))
        print(
            json.dumps(
                {"event": "started", "game": index, "room": rid, "human_role": room.game.owned_player(owner).role},
                ensure_ascii=False,
            ),
            flush=True,
        )
    manager.runner = asyncio.create_task(manager.run())
    previous = {}
    asked = set()
    chats = []
    limit = time.monotonic() + 480

    async def chat_check(room, owner):
        before = room.game.turn_sequence
        result = await manager.pet_chat(
            room.game.room_id, owner, "当前最值得追问谁？请明确区分已知事实和未证实的猜测。"
        )
        pet_checks.append(
            {
                "room": room.game.room_id,
                "before_sequence": before,
                "after_sequence": room.game.turn_sequence,
                "reply": result["pet"]["private_chat_history"][-1]["text"],
            }
        )

    try:
        while time.monotonic() < limit and not all(room.game.game_over for room, _ in games):
            for room, owner in games:
                g = room.game
                marker = (g.phase, g.turn_sequence)
                if previous.get(g.room_id) != marker:
                    previous[g.room_id] = marker
                    record = {
                        "room": g.room_id,
                        "phase": g.phase,
                        "day": g.day,
                        "actor": g.current_turn_player_id,
                        "sequence": g.turn_sequence,
                    }
                    phases.append(record)
                    print(json.dumps({"event": "phase", **record}, ensure_ascii=False), flush=True)
                if (
                    g.phase == "day_speech"
                    and g.current_turn_player_id != g.owned_player(owner).id
                    and g.room_id not in asked
                ):
                    asked.add(g.room_id)
                    chats.append(asyncio.create_task(chat_check(room, owner)))
            if router.activity["dashscope"]["requests"] > 250:
                raise RuntimeError("Playtest request budget exceeded")
            await asyncio.sleep(0.25)
        await asyncio.gather(*chats)
        report = {
            "version": "2.3.1",
            "model": router.models["dashscope"],
            "activity": router.activity["dashscope"],
            "packet_counts": dict(packet_counts),
            "live_failover": failover,
            "routing_records": list(router.records),
            "outcome_records": list(getattr(router, "outcome_records", [])),
            "sqlite_saves": dict(store.saves),
            "pet_checks": pet_checks,
            "model_actions": router.actions,
            "model_speeches": router.speeches,
            "phases": phases,
            "action_repairs": manager.ai.repairs,
            "speech_repairs": manager.ai.speech_repairs,
            "games": [
                {
                    "room_id": r.game.room_id,
                    "game_id": r.game.game_id,
                    "winner": r.game.winner,
                    "completed": r.game.game_over,
                    "day": r.game.day,
                    "elapsed_seconds": round(time.monotonic() - starts[r.game.room_id], 1),
                    "events": r.game.events,
                    "agents": [
                        {"seat": p.id, "agent_id": p.agent_id, "model_key": p.model_key, "is_human": bool(p.owner_id)}
                        for p in r.game.players
                    ],
                    "players": [
                        {"seat": p.id, "role": p.role, "private_notes": list(p.private_notes)} for p in r.game.players
                    ]
                    if r.game.game_over
                    else [],
                    "budget": manager.limits.snapshot(r.game.room_id, r.game.game_id),
                    "cost_report": router.cost_report(r.game.room_id, r.game.game_id),
                    "private_chat_count": len(r.game.pets[owner].private_chat_history),
                }
                for r, owner in games
            ],
        }
        output = ROOT / "test-artifacts" / os.getenv("LIVE_REPORT", "live-dashscope.json")
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {
                    "event": "finished",
                    "completed_games": sum(r.game.game_over for r, _ in games),
                    "activity": router.activity["dashscope"],
                    "report": str(output),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        assert all(r.game.game_over for r, _ in games), "Some live games did not finish within eight minutes"
        assert router.activity["dashscope"]["successes"] > 0, "No real provider successes"
        assert packet_counts["speech_chunk"] > 0, "No streamed public speech"
    finally:
        for task in consumers:
            task.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)
        await manager.close()
        store.close()


if __name__ == "__main__":
    asyncio.run(main())

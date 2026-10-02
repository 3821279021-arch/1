"""Authoritative room commands, scoped event delivery and independent server clocks."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any

from .ai import AIOrchestrator, memory_from
from .credentials import validate_model_options
from .game import PERSONALITIES, WerewolfGame
from .limits import Limits
from .llm import LLMRouter
from .persistence import Store
from .rules import RuleEngine
from .scope import InformationScope
from .security import password_matches

log = logging.getLogger(__name__)


@dataclass(eq=False)
class Connection:
    owner_id: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=128))


@dataclass
class Room:
    game: WerewolfGame
    engine: RuleEngine
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    connections: set[Connection] = field(default_factory=set)
    jobs: dict[tuple[int, int], asyncio.Task] = field(default_factory=dict)
    pet_tasks: set[asyncio.Task] = field(default_factory=set)
    job_started: dict[tuple[int, int], float] = field(default_factory=dict)
    offline_since: float | None = field(default_factory=time.time)
    pet_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    last_sync: float = 0
    last_access: float = field(default_factory=time.time)


class RoomManager:
    def __init__(self, store: Store, router: LLMRouter, *, time_scale: float = 1, ai_pause: float = 1.5):
        self.store, self.router = store, router
        self.router.record_sink = lambda kind, record: self.store.call("record_model", kind, record)
        self.ai = AIOrchestrator(router)
        self.limits = Limits(load=store.usage_load, persist=store.usage_save)
        self.time_scale, self.ai_pause = time_scale, ai_pause
        # Restore active clocks only. Other rooms load when their members visit.
        self.rooms = {g.room_id: self._room(g) for g in store.load_active()}
        self.runner: asyncio.Task | None = None
        self.last_maintenance = 0.0
        self.unload_seconds = float(os.getenv("ROOM_IDLE_UNLOAD_SECONDS", "300"))
        self.lobby_ttl = float(os.getenv("ROOM_LOBBY_TTL_HOURS", "6")) * 3600
        self.finished_ttl = float(os.getenv("ROOM_FINISHED_TTL_DAYS", "14")) * 86400
        self.suspended_ttl = float(os.getenv("ROOM_SUSPENDED_TTL_HOURS", "48")) * 3600
        self.reconnect_grace = max(0, float(os.getenv("ROOM_RECONNECT_GRACE_SECONDS", "45")))

    def _room(self, game: WerewolfGame) -> Room:
        from .memory import build_memory_from_view, compact_memory

        for p in game.players:
            if not p.memory.get("schema_version"):
                p.memory = build_memory_from_view(InformationScope.player_view(game, p.id), p.memory)
            else:
                p.memory = compact_memory(p.memory, game.day)
        return Room(game, RuleEngine(game, time_scale=self.time_scale))

    def get(self, room_id: str) -> Room:
        room = self.rooms.get(room_id)
        if not room:
            game = self.store.get(room_id)
            if not game or game.lifecycle == "DELETED":
                raise ValueError("房间不存在")
            room = self.rooms[room_id] = self._room(game)
        room.last_access = time.time()
        return room

    def require(self, room_id: str, owner_id: str) -> Room:
        room = self.get(room_id)
        if not room.game.owned_player(owner_id):
            raise PermissionError("你不属于此房间")
        return room

    async def get_async(self, room_id: str) -> Room:
        if room_id not in self.rooms:
            game = await self.store.call("get", room_id)
            if not game or game.lifecycle == "DELETED":
                raise ValueError("房间不存在")
            # Concurrent readers share the same lock and scheduler instance.
            self.rooms.setdefault(room_id, self._room(game))
        room = self.rooms[room_id]
        room.last_access = time.time()
        return room

    async def require_async(self, room_id: str, owner_id: str) -> Room:
        room = await self.get_async(room_id)
        if not room.game.owned_player(owner_id):
            raise PermissionError("你不属于此房间")
        return room

    def snapshot(self, room: Room, owner_id: str) -> dict[str, Any]:
        data = InformationScope.owner_view(room.game, owner_id)
        data["provider_status"] = self.router.status()
        data["model_registry"] = self.router.model_status()
        connected = {c.owner_id for c in room.connections}
        for player in data["players"]:
            actor = room.game.player(player["id"])
            player["connected"] = actor.owner_id in connected if actor.owner_id else True
            public_phase = room.game.phase in {"day_speech", "day_vote", "last_words"}
            player["thinking"] = public_phase and any(
                key[1] == actor.id and not job.done() for key, job in room.jobs.items()
            )
            player["thinking_started_at"] = (
                room.job_started.get((room.game.turn_sequence, actor.id)) if player["thinking"] else None
            )
        keys: dict[str, list[int]] = {}
        for p in room.game.players:
            if not p.owner_id and not p.model_key.startswith("mock:"):
                keys.setdefault(p.model_key, []).append(p.id)
        data["shared_models"] = {key: seats for key, seats in keys.items() if len(seats) > 1}
        if data["is_host"]:
            data["ai_budget"] = self.limits.snapshot(room.game.room_id, room.game.game_id)
        return data

    def push(self, connection: Connection, packet: dict[str, Any]) -> None:
        if connection.queue.full():
            while not connection.queue.empty():
                connection.queue.get_nowait()
            connection.queue.put_nowait({"type": "reconnect", "data": {}})
            return
        connection.queue.put_nowait(packet)

    def commit(self, room: Room, *, save: bool = True, snapshot: bool = True) -> None:
        if save:
            self.store.save(room.game, room.engine.outbox)
        events, room.engine.outbox = room.engine.outbox, []
        for connection in tuple(room.connections):
            if not room.game.owned_player(connection.owner_id):
                self.push(connection, {"type": "room_left", "data": {"room_id": room.game.room_id}})
                self.push(connection, {"type": "reconnect", "data": {}})
                room.connections.discard(connection)
                continue
            for event in events:
                if InformationScope.permits(room.game, connection.owner_id, event):
                    packet = {
                        k: deepcopy(event[k])
                        for k in (
                            "type",
                            "data",
                            "game_id",
                            "turn_id",
                            "turn_sequence",
                            "state_revision",
                            "event_id",
                            "seq",
                        )
                    }
                    self.push(connection, InformationScope.client_event(room.game, packet))
            if snapshot:
                self.push(connection, {"type": "state_snapshot", "data": self.snapshot(room, connection.owner_id)})

    async def commit_async(self, room: Room, *, save: bool = True, snapshot: bool = True) -> None:
        if save:
            # Await the write even when cancelled: never release a room lock while
            # its mutable snapshot is still being read by the storage thread.
            saving = asyncio.create_task(self.store.call("save", room.game, list(room.engine.outbox)))
            try:
                await asyncio.shield(saving)
            except asyncio.CancelledError:
                await saving
                raise
        self.commit(room, save=False, snapshot=snapshot)

    async def rate(self, kind: str, room_id: str, owner_id: str) -> None:
        await asyncio.to_thread(self.limits.check, kind, "room:" + room_id)
        await asyncio.to_thread(self.limits.check, kind, "session:" + owner_id)

    def disconnect_identity(self, owner_id: str) -> None:
        for room in self.rooms.values():
            for connection in tuple(room.connections):
                if connection.owner_id == owner_id:
                    self.push(connection, {"type": "session_revoked", "data": {}})
                    room.connections.discard(connection)
                    if not room.connections:
                        room.offline_since = time.time()

    async def unsubscribe(self, room_id: str, connection: Connection) -> None:
        room = self.rooms.get(room_id)
        if room:
            async with room.lock:
                room.connections.discard(connection)
                if not room.connections and room.offline_since is None:
                    room.offline_since = time.time()

    def cancel_jobs(self, room: Room) -> None:
        current = asyncio.current_task()
        for task in (*room.jobs.values(), *room.pet_tasks):
            if task is not current:
                task.cancel()
        room.jobs.clear()
        room.job_started.clear()

    def check_presence(self, room: Room, now: float | None = None, *, immediate: bool = False) -> bool:
        now = time.time() if now is None else now
        if room.connections:
            room.offline_since = None
            return False
        if room.offline_since is None:
            room.offline_since = now
        if immediate or now - room.offline_since >= self.reconnect_grace:
            if room.engine.suspend(now):
                self.cancel_jobs(room)
                return True
        return False

    def invalidate_credential(self, owner_id: str, credential_id: str) -> None:
        """Abort jobs whose credential was deleted or replaced, then reschedule safely."""
        for room in self.rooms.values():
            for player in room.game.players:
                if (
                    getattr(player, "credential_owner_id", None) == owner_id
                    and getattr(player, "credential_id", None) == credential_id
                ):
                    for key, task in list(room.jobs.items()):
                        if key[1] == player.id or key[1] == 0:
                            task.cancel()
                            room.jobs.pop(key, None)

    async def analysis(
        self, room_id: str, owner_id: str, game_id: str | None = None, *, historical_only: bool = False
    ) -> dict[str, Any]:
        if historical_only:
            requested = game_id
            game = await self.store.call("completed_game", room_id, requested)
            if game is None:
                raise ValueError("已完成的对局不存在")
            if not game.owned_player(owner_id):
                raise PermissionError("你未参与此对局")
        else:
            room = await self.require_async(room_id, owner_id)
            async with room.lock:
                requested = game_id or room.game.game_id
                if requested == room.game.game_id:
                    if not room.game.game_over:
                        raise PermissionError("对局结束后才能查看身份、竞技统计与回放")
                    game = deepcopy(room.game)
                else:
                    game = await self.store.call("completed_game", room_id, requested)
                    if game is None:
                        raise ValueError("已完成的对局不存在")
                if not game.owned_player(owner_id):
                    raise PermissionError("你未参与此对局")
        if self.router._background_writes:
            await asyncio.gather(*self.router._background_writes, return_exceptions=True)
        telemetry = await self.store.call("model_report", room_id, requested)
        events = await self.store.call("public_replay", room_id, requested)
        from .analysis import analyze_public_speeches
        from .roles import ROLE_DEFINITIONS

        speech_analysis = analyze_public_speeches(game.events)
        metrics = []
        actions = getattr(game, "action_history", [])
        for player in game.players:
            calls = [record for record in telemetry["calls"] if record.get("agent_id") == player.agent_id]
            outcomes = [record for record in telemetry["outcomes"] if record.get("agent_id") == player.agent_id]
            own_actions = [record for record in actions if record.get("player_id") == player.id]
            votes = [record for record in own_actions if record.get("action") == "vote"]
            hits = sum(
                1
                for record in votes
                if record.get("target") and ROLE_DEFINITIONS[game.player(record["target"]).role].faction == "wolves"
            )
            speeches = [
                event for event in game.events if event.get("kind") == "speech" and event.get("player_id") == player.id
            ]
            faction = ROLE_DEFINITIONS[player.role].faction
            win_faction = (
                "wolves"
                if game.winner in {"wolves", "wolf", "狼人"}
                else "good"
                if game.winner in {"good", "好人"}
                else None
            )
            metrics.append(
                {
                    "id": player.id,
                    "name": player.name,
                    "model": player.model if not player.owner_id else "真人",
                    "role": player.role,
                    "role_name": ROLE_DEFINITIONS[player.role].display_name,
                    "faction": faction,
                    "won": faction == win_faction if win_faction else None,
                    "speeches": len(speeches),
                    "votes": len(votes),
                    "vote_hits": hits,
                    "vote_accuracy": round(hits / len(votes), 3) if votes else None,
                    "actions": len(own_actions),
                    "skills": sum(
                        record.get("action") not in {"speech", "vote", "wolf_discuss"} for record in own_actions
                    ),
                    "action_history": own_actions,
                    "latency_ms": round(sum(record.get("latency_ms", 0) for record in calls) / len(calls))
                    if calls
                    else 0,
                    "input_tokens": sum(
                        record.get("input_tokens", record.get("estimated_input_tokens", 0)) for record in calls
                    ),
                    "output_tokens": sum(record.get("output_tokens", 0) for record in calls),
                    "estimated_cost": round(sum(record.get("estimated_cost", 0) for record in calls), 6)
                    if any("estimated_cost" in record for record in calls)
                    else 0
                    if outcomes and all(record.get("provider_used") == "mock" for record in outcomes)
                    else None,
                    "errors": sum(not record.get("success", False) for record in calls),
                    "fallbacks": sum(
                        record.get("status") == "fallback" or bool(record.get("failure_reason")) for record in outcomes
                    ),
                    "calls": len(calls),
                    "first_token_ms": round(
                        sum(r["first_token_ms"] for r in calls if "first_token_ms" in r)
                        / sum("first_token_ms" in r for r in calls)
                    )
                    if any("first_token_ms" in r for r in calls)
                    else None,
                    "generation_ms": sum(r.get("generation_ms", 0) for r in calls),
                    "output_characters": sum(r.get("output_characters", 0) for r in calls),
                    "actual_models": sorted(
                        {record.get("model_key") for record in outcomes if record.get("model_key")}
                    ),
                    **speech_analysis["players"].get(str(player.id), {}),
                }
            )
        return {
            "game_id": requested,
            "winner": game.winner,
            "mode": game.mode,
            "player_count": game.player_count,
            "players": metrics,
            "events": events,
            "speech_analysis_note": speech_analysis["note"],
            "usage_note": "无上游 usage 时输入 token 为估算；费用按部署者配置单价估算，Mock 不代表真实模型能力。",
        }

    async def create(
        self,
        owner_id: str,
        name: str,
        seat: int | None = None,
        title: str = "月下狼人杀",
        pace: str = "standard",
        action_id: str | None = None,
        *,
        mode: str = "quick6",
        player_count: int | None = None,
        roles: list[str] | None = None,
        board_policy: str = "fixed",
        random_role_pool: list[str] | None = None,
    ) -> dict[str, Any]:
        existing = await self.store.call("created", owner_id, action_id) if action_id else None
        if existing:
            return self.snapshot(await self.get_async(existing.room_id), owner_id)
        await asyncio.to_thread(self.limits.check, "room", owner_id)
        game = WerewolfGame(secrets.token_hex(5), owner_id, title=title, pace=pace, creation_action_id=action_id)
        room = self._room(game)
        room.engine.configure(
            owner_id,
            pace,
            [],
            mode=mode,
            player_count=player_count,
            roles=roles,
            board_policy=board_policy,
            random_role_pool=random_role_pool,
        )
        room.engine.join(owner_id, name, seat)
        log.info("room_created room_id=%s request_id=%s", game.room_id, action_id)
        self.rooms[game.room_id] = room
        await self.commit_async(room)
        return self.snapshot(room, owner_id)

    async def join(
        self, room_id: str, owner_id: str, name: str, seat: int | None = None, password: str = ""
    ) -> dict[str, Any]:
        room = await self.get_async(room_id)
        async with room.lock:
            if room.game.lifecycle != "LOBBY":
                raise ValueError("此房间已开局或已关闭")
            if not room.game.owned_player(owner_id):
                await self.rate("join", room_id, owner_id)
                if room.game.locked or owner_id in room.game.blocked_owners:
                    raise PermissionError("房间已锁定或房主禁止此次加入")
                if not await asyncio.to_thread(password_matches, password, room.game.password_hash):
                    raise PermissionError("房间密码不正确")
                room.engine.join(owner_id, name, seat)
                await self.commit_async(room)
            return self.snapshot(room, owner_id)

    def acknowledgement(self, room: Room, owner_id: str, stored: dict, replay: bool) -> dict:
        result = self.snapshot(room, owner_id)
        result["action_ack"] = {**stored, "replayed": replay}
        return result

    async def command(self, room_id: str, owner_id: str, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        room = await self.get_async(room_id)
        async with room.lock:
            action_id = payload.get("action_id")
            cache_key = f"{owner_id}:{command}:{action_id}" if action_id else None
            if cache_key and cache_key in room.game.processed_actions:
                if command == "leave":
                    return {
                        "left": True,
                        "room_id": room_id,
                        "action_ack": {**room.game.processed_actions[cache_key], "replayed": True},
                    }
                return self.acknowledgement(room, owner_id, room.game.processed_actions[cache_key], True)
            if not room.game.owned_player(owner_id):
                raise PermissionError("你不属于此房间")
            await self.rate("chat" if command == "wolf_chat" else "action", room_id, owner_id)
            if room.game.lifecycle in {"ARCHIVED", "DELETED"}:
                raise ValueError("房间已关闭，只能查看历史")
            if room.game.lifecycle == "SUSPENDED" and command not in {"resume", "leave", "close", "delete"}:
                raise ValueError("对局已暂停，请先继续对局")
            if room.engine.tick():
                await self.commit_async(room)
            if command != "action":
                if payload.get("game_id") and payload["game_id"] != room.game.game_id:
                    raise ValueError("对局已变更，请同步")
                if (
                    payload.get("expected_state_revision") is not None
                    and payload["expected_state_revision"] != room.game.state_revision
                ):
                    raise ValueError("状态版本已变更，请同步后重试")
            if command == "action":
                p = room.game.owned_player(owner_id)
                room.engine.apply(p.id, payload)
                if room.game.pets[owner_id].delegate_next:
                    room.engine.consume_delegate(owner_id)
            elif command == "start":
                self.limits.check_active_rooms(sum(r.game.lifecycle == "ACTIVE" for r in self.rooms.values()))
                presets = [
                    {
                        "id": seat,
                        "provider": "mock",
                        "personality": list(PERSONALITIES)[(seat - 1) % len(PERSONALITIES)],
                        **room.game.seat_presets.get(str(seat), {}),
                    }
                    for seat in range(1, room.game.player_count + 1)
                    if not any(p.id == seat for p in room.game.players)
                ]
                platform = [entry for entry in presets if not entry.get("credential_id")]
                assignments = self.router.registry.allocate(platform, unique=room.game.unique_model_per_ai_seat)
                selected = set(assignments.values()) - {"mock:mock"}
                for entry in presets:
                    if entry.get("credential_id"):
                        binding = self.router.credentials.validate_route(
                            entry["credential_owner_id"], entry["credential_id"], entry["model_id"], scope_id=room_id
                        )
                        key = binding["model_key"]
                        if room.game.unique_model_per_ai_seat and key in selected:
                            raise ValueError("独立模型模式下，每个 AI 座位须使用不同模型 ID；可在大厅关闭独立模型限制")
                        selected.add(key)
                        assignments[entry["id"]] = key
                room.engine.start(owner_id, model_assignments=assignments)
            elif command in {"lock", "password", "kick", "reopen"}:
                await asyncio.to_thread(room.engine.moderate, owner_id, command, payload)
            elif command == "configure":
                seats = deepcopy(payload["seats"])
                for entry in seats:
                    entry.pop("credential_owner_id", None)
                    if entry.get("credential_id") is not None and not isinstance(entry["credential_id"], str):
                        raise ValueError("凭据标识格式无效")
                    entry["model_options"] = validate_model_options(entry.get("model_options"))
                    if entry.get("credential_id"):
                        binding = self.router.credentials.validate_route(
                            owner_id, entry["credential_id"], entry.get("model_id", ""), scope_id=room_id
                        )
                        entry.update(
                            credential_owner_id=owner_id,
                            provider=binding["provider"],
                            model_id=binding["model_id"],
                            model_key=binding["model_key"],
                        )
                room.engine.configure(
                    owner_id,
                    payload["pace"],
                    seats,
                    payload.get("unique_model_per_ai_seat"),
                    mode=payload.get("mode"),
                    player_count=payload.get("player_count"),
                    roles=payload.get("roles"),
                    board_policy=payload.get("board_policy"),
                    random_role_pool=payload.get("random_role_pool"),
                )
            elif command == "seat":
                room.engine.change_seat(owner_id, payload.get("seat"))
            elif command == "wolf_chat":
                room.engine.wolf_message(room.game.owned_player(owner_id).id, payload["text"])
            elif command == "pet_config":
                room.engine.configure_pet(owner_id, payload)
                pet = room.game.pets[owner_id]
                if pet.control_mode != "autopilot" and not pet.delegate_next:
                    pid = room.game.owned_player(owner_id).id
                    for key, task in list(room.jobs.items()):
                        if key[1] == pid and not task.done():
                            task.cancel()
                            room.jobs.pop(key, None)
            elif command == "leave":
                if room.game.lifecycle in {"ACTIVE", "SUSPENDED", "FINISHED"}:
                    for connection in tuple(room.connections):
                        if connection.owner_id == owner_id:
                            self.push(connection, {"type": "room_left", "data": {"room_id": room_id}})
                            self.push(connection, {"type": "reconnect", "data": {}})
                            room.connections.discard(connection)
                    self.check_presence(room, immediate=True)
                else:
                    room.engine.leave(owner_id)
                if cache_key:
                    room.game.processed_actions[cache_key] = {
                        "action_id": action_id,
                        "game_id": room.game.game_id,
                        "state_revision": room.game.state_revision,
                    }
                    if len(room.game.processed_actions) > 5000:
                        room.game.processed_actions.pop(next(iter(room.game.processed_actions)))
                await self.commit_async(room)
                return {"left": True, "room_id": room_id}
            elif command == "pause":
                if room.game.host_id != owner_id:
                    raise PermissionError("只有房主可以暂停对局")
                if not room.engine.suspend():
                    raise ValueError("只有进行中的对局可以暂停")
                self.cancel_jobs(room)
            elif command == "resume":
                room.engine.resume(owner_id)
                room.offline_since = None if room.connections else time.time()
            elif command == "delete":
                if room.game.host_id != owner_id:
                    raise PermissionError("只有房主可以关闭并删除房间")
                self.cancel_jobs(room)
                await self.store.call("delete_room", room_id)
                room.game.lifecycle = "DELETED"
                for connection in tuple(room.connections):
                    self.push(connection, {"type": "room_left", "data": {"room_id": room_id}})
                    self.push(connection, {"type": "reconnect", "data": {}})
                self.rooms.pop(room_id, None)
                if self.router.credentials:
                    self.router.credentials.clear_scope(room_id)
                return {"deleted": True, "room_id": room_id}
            elif command == "close":
                room.engine.close_room(owner_id)
                self.cancel_jobs(room)
                if getattr(self.router, "credentials", None):
                    self.router.credentials.clear_scope(room_id)
            elif command == "rematch":
                room.engine.rematch(owner_id)
                self.cancel_jobs(room)
            else:
                raise ValueError("未知命令")
            stored = {"action_id": action_id, "game_id": room.game.game_id, "state_revision": room.game.state_revision}
            if cache_key:
                room.game.processed_actions[cache_key] = stored
                if len(room.game.processed_actions) > 5000:
                    room.game.processed_actions.pop(next(iter(room.game.processed_actions)))
            await self.commit_async(room)
            return self.acknowledgement(room, owner_id, stored, False) if cache_key else self.snapshot(room, owner_id)

    async def pet_chat(
        self, room_id: str, owner_id: str, text: str, client_message_id: str | None = None
    ) -> dict[str, Any]:
        room = await self.require_async(room_id, owner_id)
        if room.game.lifecycle == "SUSPENDED":
            raise ValueError("对局已暂停，请先继续对局")
        task = asyncio.current_task()
        room.pet_tasks.add(task)
        try:
            return await self._pet_chat(room_id, owner_id, text, client_message_id)
        finally:
            room.pet_tasks.discard(task)

    async def _pet_chat(
        self, room_id: str, owner_id: str, text: str, client_message_id: str | None = None
    ) -> dict[str, Any]:
        room = await self.require_async(room_id, owner_id)
        pet_lock = room.pet_locks.setdefault(owner_id, asyncio.Lock())
        if client_message_id and any(
            entry.get("client_message_id") == client_message_id and entry["role"] == "assistant"
            for entry in room.game.pets[owner_id].private_chat_history
        ):
            return self.snapshot(room, owner_id)
        if pet_lock.locked():
            raise ValueError("搭档正在回复上一条消息")
        if room.game.lifecycle in {"ARCHIVED", "DELETED"}:
            raise ValueError("房间已关闭")
        await self.rate("chat", room_id, owner_id)
        await asyncio.to_thread(self.limits.check, "pet", f"{owner_id}:{room_id}")
        async with pet_lock:
            async with room.lock:
                room.engine.pet_message(owner_id, "user", text, client_message_id)
                await self.commit_async(room)
                pid = room.game.owned_player(owner_id).id
                view = InformationScope.ai_view(room.game, pid)
                pet = asdict(room.game.pets[owner_id])
            try:
                with self.router.request_scope(
                    room_id=room_id,
                    game_id=view["game_id"],
                    agent_id=pet["agent_id"],
                    category="pet_chat",
                    day=view["day"],
                    phase=view["phase"],
                    request_id=client_message_id,
                    guard=self.limits,
                ):
                    reply, memory = await asyncio.wait_for(
                        self.ai.pet_reply(view, pet, text, await self.store.call("memory", owner_id)),
                        timeout=min(self.router.timeout * 3 + 1, 45),
                    )
            except Exception as exc:
                log.warning("Pet provider unavailable: %s", type(exc).__name__)
                reply = "搭档暂时无法连接模型。建议先核对公开发言与票型，避免把猜测当成事实。"
                memory = memory_from(view)
            async with room.lock:
                if room.game.game_id != view["game_id"] or room.game.lifecycle in {"ARCHIVED", "DELETED", "SUSPENDED"}:
                    log.info("stale_ai_response room=%s agent=%s category=pet_chat", room_id, pet["agent_id"])
                    return self.snapshot(room, owner_id)
                room.engine.pet_message(owner_id, "assistant", reply, client_message_id)
                room.engine.pet_memory(owner_id, memory)
                room.game.pets[owner_id].execution_status = self.router.execution(pet["agent_id"])
                await self.commit_async(room)
                return self.snapshot(room, owner_id)

    async def subscribe(self, room_id: str, owner_id: str, last_event_id: str | None = None) -> Connection:
        room = await self.require_async(room_id, owner_id)
        await self.rate("reconnect", room_id, owner_id)
        async with room.lock:
            connection = Connection(owner_id)
            room.connections.add(connection)
            room.offline_since = None
            self.push(
                connection,
                {
                    "type": "state_snapshot",
                    "data": self.snapshot(room, owner_id),
                    "sync": {"mode": "authoritative_snapshot", "last_event_id": last_event_id},
                },
            )
            return connection

    def can_control(self, room: Room, pid: int) -> bool:
        p = room.game.player(pid)
        if not p.owner_id:
            return True
        pet = room.game.pets[p.owner_id]
        return pet.control_mode == "autopilot" or pet.delegate_next

    def applicable(self, room: Room, pid: int, view: dict, discussion: bool = False) -> bool:
        g = room.game
        valid = (
            g.game_id == view["game_id"]
            and g.turn_id == view["turn_id"]
            and g.lifecycle == "ACTIVE"
            and self.can_control(room, pid)
        )
        if valid:
            g.player(pid)
            valid = bool(room.engine.action_for(pid))
            route = view.get("_credential_route")
            if valid and route and route.get("revision") is not None:
                valid = self.router.credentials.route_valid(
                    route["owner_id"], route["id"], route["revision"], scope_id=g.room_id
                )
        if not valid:
            log.info(
                "stale_ai_response room=%s game=%s turn=%s revision=%s",
                g.room_id,
                view["game_id"],
                view["turn_id"],
                view["state_revision"],
            )
        return valid

    def execution(self, room: Room, pid: int, agent_id: str, category: str) -> dict:
        status = self.router.execution(agent_id)
        p = room.game.player(pid)
        if p.owner_id:
            room.game.pets[p.owner_id].execution_status = status
        elif category in {"speech", "vote"}:
            p.execution_status = status
        else:
            p.conversation_state["last_execution"] = status
        if status:
            # Revealing which seat calls a model in a skill phase would reveal its
            # hidden role. Only public actions produce public model labels.
            audience = "public" if not p.owner_id and category in {"speech", "vote"} else "player"
            room.engine.emit("model_execution", {"player_id": pid, **status}, audience, pid)
        return status

    async def actor(self, room: Room, pid: int, sequence: int, discussion: bool = False) -> None:
        view = None
        try:
            async with room.lock:
                if room.game.turn_sequence != sequence or not self.can_control(room, pid):
                    return
                p = room.game.player(pid)
                view = InformationScope.ai_view(room.game, pid)
                memory = memory_from(view, p.memory)
                if p.owner_id:
                    pet = room.game.pets[p.owner_id]
                    provider, personality, style, agent_id = (
                        pet.model_key or pet.provider,
                        pet.personality,
                        dict(pet.play_style),
                        pet.agent_id,
                    )
                else:
                    provider, personality, style, agent_id = p.model_key or p.provider, p.personality, {}, p.agent_id
                route_metadata = {"model_parameters": getattr(p, "model_options", {})}
                if getattr(p, "credential_id", None):
                    route_metadata = {
                        "credential_id": p.credential_id,
                        "credential_owner_id": p.credential_owner_id,
                        "credential_scope_id": room.game.room_id,
                        "model_id": p.model_id or p.model,
                        "model_parameters": getattr(p, "model_options", {}),
                    }
                    try:
                        credential = self.router.credentials.get(
                            p.credential_owner_id, p.credential_id, scope_id=room.game.room_id
                        )
                        route_metadata["credential_revision"] = credential.get("revision")
                        view["_credential_route"] = {
                            "id": p.credential_id,
                            "owner_id": p.credential_owner_id,
                            "revision": credential.get("revision"),
                        }
                    except (ValueError, PermissionError):
                        # A deleted temporary credential is an explicit failed route;
                        # the router records neutral fallback, without borrowing a key.
                        view["_credential_route"] = {
                            "id": p.credential_id,
                            "owner_id": p.credential_owner_id,
                            "revision": None,
                        }
            category = "wolf_discussion" if discussion else view["pending_action"]["type"]
            with self.router.request_scope(
                room_id=room.game.room_id,
                game_id=view["game_id"],
                agent_id=agent_id,
                category=category,
                day=view["day"],
                phase=view["phase"],
                request_id=f"{view['turn_id']}:{pid}",
                guard=self.limits,
                **route_metadata,
            ):
                if discussion:
                    message = await self.ai.wolf_discuss(view, provider, personality, memory, style)
                    async with room.lock:
                        if not self.applicable(room, pid, view, True):
                            return
                        room.engine.wolf_message(pid, message)
                        self.execution(room, pid, agent_id, category)
                        await self.commit_async(room)
                    return
                if category in {"speech", "vote"} and view.get("secondary_actions"):
                    with self.router.request_scope(category="secondary_skill"):
                        extra = await self.ai.choose_secondary(view, provider, personality, memory, style)
                    if extra:
                        async with room.lock:
                            if not self.applicable(room, pid, view):
                                return
                            room.engine.apply(pid, extra)
                            self.execution(room, pid, agent_id, "secondary_skill")
                            await self.commit_async(room)
                            fresh = InformationScope.ai_view(room.game, pid)
                            if fresh["turn_id"] != view["turn_id"] or not fresh.get("pending_action"):
                                return
                            fresh["_credential_route"] = view.get("_credential_route")
                            view = fresh
                if category == "speech":
                    async for chunk in self.ai.speak(view, provider, personality, memory, style):
                        async with room.lock:
                            if not self.applicable(room, pid, view) or not room.engine.append_speech(
                                pid, chunk, sequence
                            ):
                                return
                            # Hot path: event only, no full state serialization or SQLite write.
                            await self.commit_async(room, save=False, snapshot=False)
                    if self.ai_pause:
                        await asyncio.sleep(min(self.ai_pause, 0.8))
                    async with room.lock:
                        if not self.applicable(room, pid, view):
                            return
                        payload = {
                            "action": "speech",
                            "speech": room.game.current_speech or "我先听后续发言。",
                            "game_id": view["game_id"],
                            "turn_sequence": sequence,
                            "turn_id": view["turn_id"],
                        }
                        execution = self.execution(room, pid, agent_id, category)
                        if execution.get("status") == "partial":
                            room.engine.emit(
                                "speech_error",
                                {"player_id": pid, "partial": True, "message": "模型输出中断，已保留文字"},
                            )
                        room.engine.apply(pid, payload)
                        if p.owner_id:
                            room.engine.consume_delegate(p.owner_id)
                        await self.commit_async(room)
                else:
                    proposal = await self.ai.propose(view, provider, personality, memory, style)
                    async with room.lock:
                        if not self.applicable(room, pid, view):
                            return
                        room.engine.apply(pid, proposal)
                        self.execution(room, pid, agent_id, category)
                        if p.owner_id:
                            room.engine.consume_delegate(p.owner_id)
                        await self.commit_async(room)
        except asyncio.CancelledError:
            if view:
                log.info("stale_ai_response cancelled room=%s turn=%s", room.game.room_id, view["turn_id"])
            raise
        except ValueError:
            if view:
                log.info("stale_ai_response rejected room=%s turn=%s", room.game.room_id, view["turn_id"])
        except Exception as exc:
            log.warning("AI action failed room=%s error=%s", room.game.room_id, type(exc).__name__)
            async with room.lock:
                if view and self.applicable(room, pid, view):
                    pending = room.engine.action_for(pid)
                    payload = {
                        "action": pending["type"],
                        "game_id": view["game_id"],
                        "turn_sequence": sequence,
                        "turn_id": view["turn_id"],
                        "target": None,
                        "save": False,
                        "poison_target": None,
                    }
                    if pending["type"] == "speech":
                        payload["speech"] = room.game.current_speech or "（模型连接失败，本轮跳过）"
                        room.engine.emit(
                            "speech_error",
                            {
                                "player_id": pid,
                                "partial": bool(room.game.current_speech),
                                "message": "模型连接失败，已结束本轮",
                            },
                        )
                    room.engine.apply(pid, payload)
                    await self.commit_async(room)

    async def discussion_rounds(self, room: Room, sequence: int) -> None:
        # The second wolf sees the first one's completed message; the next round
        # can revise the plan. Human wolf messages may arrive between responses.
        for _ in range(2):
            for p in list(room.game.players):
                if room.game.turn_sequence != sequence:
                    return
                pending = room.engine.action_for(p.id)
                if pending and pending["type"] == "wolf_discuss" and self.can_control(room, p.id):
                    await self.actor(room, p.id, sequence, True)
        async with room.lock:
            if room.game.turn_sequence != sequence:
                return
            for p in list(room.game.players):
                pending = room.engine.action_for(p.id)
                if pending and pending["type"] == "wolf_discuss" and self.can_control(room, p.id):
                    room.engine.apply(
                        p.id,
                        {
                            "action": "wolf_discuss",
                            "game_id": room.game.game_id,
                            "turn_sequence": sequence,
                            "turn_id": room.game.turn_id,
                        },
                    )
            await self.commit_async(room)

    def launch_jobs(self, room: Room) -> None:
        g = room.game
        for key, task in list(room.jobs.items()):
            if key[0] != g.turn_sequence:
                if not task.done():
                    task.cancel()
                room.jobs.pop(key, None)
                room.job_started.pop(key, None)
        if g.lifecycle != "ACTIVE" or g.phase in {"lobby", "finished"}:
            return
        if g.phase == "night_discussion":
            key = (g.turn_sequence, 0)
            if key not in room.jobs:
                room.jobs[key] = asyncio.create_task(self.discussion_rounds(room, g.turn_sequence))
                room.job_started[key] = time.time()
            return
        for p in g.players:
            if room.engine.action_for(p.id) and self.can_control(room, p.id):
                key = (g.turn_sequence, p.id)
                if key not in room.jobs:
                    room.jobs[key] = asyncio.create_task(self.actor(room, p.id, g.turn_sequence))
                    room.job_started[key] = time.time()

    def maintenance(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        protected = {rid for rid, r in self.rooms.items() if r.connections or r.game.lifecycle == "ACTIVE"}
        archived = self.store.archive_expired(
            now=now,
            lobby_seconds=self.lobby_ttl,
            finished_seconds=self.finished_ttl,
            suspended_seconds=self.suspended_ttl,
            exclude=protected,
        )
        for rid in archived:
            if rid in self.rooms:
                restored = self.store.get(rid)
                self.rooms[rid].game = restored
                self.rooms[rid].engine = RuleEngine(restored, time_scale=self.time_scale)
        purged = self.store.cleanup_expired_rooms(
            now=now, exclude={rid for rid in protected if self.rooms[rid].game.lifecycle != "ARCHIVED"}
        )
        self.store.cleanup_expired_sessions(now=now)
        for rid in purged:
            expired = self.rooms.pop(rid, None)
            if expired:
                for connection in expired.connections:
                    self.push(connection, {"type": "room_left", "data": {"room_id": rid}})
                    self.push(connection, {"type": "reconnect", "data": {}})
        for rid, room in list(self.rooms.items()):
            if (
                room.game.lifecycle != "ACTIVE"
                and not room.connections
                and now - room.last_access >= self.unload_seconds
                and not room.lock.locked()
            ):
                if rid not in archived:
                    self.store.save(room.game)
                self.rooms.pop(rid, None)

    async def maintenance_async(self) -> None:
        now = time.time()
        # Hold loaded idle rooms during archival; state mutations stay on the loop.
        locked = []
        try:
            for room in list(self.rooms.values()):
                if room.game.lifecycle != "ACTIVE" and not room.lock.locked():
                    await room.lock.acquire()
                    locked.append(room)
            protected = {
                rid for rid, r in self.rooms.items() if r.connections or r.game.lifecycle == "ACTIVE" or r not in locked
            }
            archived = await self.store.call(
                "archive_expired",
                now=now,
                lobby_seconds=self.lobby_ttl,
                finished_seconds=self.finished_ttl,
                suspended_seconds=self.suspended_ttl,
                exclude=protected,
            )
            for rid in archived:
                if rid in self.rooms:
                    restored = await self.store.call("get", rid)
                    self.rooms[rid].game = restored
                    self.rooms[rid].engine = RuleEngine(restored, time_scale=self.time_scale)
            purged = await self.store.call(
                "cleanup_expired_rooms",
                now=now,
                exclude={rid for rid, room in self.rooms.items() if room not in locked},
            )
            await self.store.call("cleanup_expired_sessions", now=now)
            for rid in purged:
                expired = self.rooms.pop(rid, None)
                if expired:
                    for connection in expired.connections:
                        self.push(connection, {"type": "room_left", "data": {"room_id": rid}})
                        self.push(connection, {"type": "reconnect", "data": {}})
            for rid, room in list(self.rooms.items()):
                if room in locked and not room.connections and now - room.last_access >= self.unload_seconds:
                    if rid not in archived:
                        await self.store.call("save", room.game)
                    self.rooms.pop(rid, None)
        finally:
            for room in locked:
                room.lock.release()

    async def run(self) -> None:
        while True:
            for room in list(self.rooms.values()):
                if room.game.lifecycle != "ACTIVE":
                    continue
                try:
                    async with room.lock:
                        if self.check_presence(room):
                            await self.commit_async(room)
                            continue
                        changed = room.engine.tick()
                        now = time.time()
                        if changed:
                            await self.commit_async(room)
                        elif room.connections and now - room.last_sync >= 5:
                            room.engine.emit(
                                "timer_sync",
                                {"server_time": now, "turn_deadline": room.game.turn_deadline},
                                mutation=False,
                            )
                            await self.commit_async(room, save=False, snapshot=False)
                            room.last_sync = now
                        old_jobs = set(room.jobs)
                        self.launch_jobs(room)
                        if set(room.jobs) - old_jobs and room.connections:
                            await self.commit_async(room, save=False)
                except Exception as exc:
                    log.error("Room clock error room=%s error=%s", room.game.room_id, type(exc).__name__)
            if time.time() - self.last_maintenance >= 30:
                # Maintenance runs serially between scheduler ticks; storage IO leaves the loop.
                await self.maintenance_async()
                self.last_maintenance = time.time()
            await asyncio.sleep(min(0.2, max(0.005, self.time_scale * 0.2)))

    async def close(self) -> None:
        tasks = [task for room in self.rooms.values() for task in (*room.jobs.values(), *room.pet_tasks)]
        if self.runner:
            tasks.append(self.runner)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for room in self.rooms.values():
            await self.store.call("save", room.game, room.engine.outbox)
        await self.router.close()

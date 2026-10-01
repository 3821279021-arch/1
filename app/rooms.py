"""Authoritative room commands, scoped event delivery and independent server clocks."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import logging
import os
import secrets
import time
from typing import Any

from .ai import AIOrchestrator, memory_from
from .game import PERSONALITIES, WerewolfGame
from .limits import Limits
from .llm import LLMRouter
from .persistence import Store
from .rules import RuleEngine
from .scope import InformationScope
from .security import password_matches
from .strategy import GameBeliefState

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
        self.lobby_ttl = float(os.getenv("ROOM_LOBBY_TTL_HOURS", "6"))*3600
        self.finished_ttl = float(os.getenv("ROOM_FINISHED_TTL_DAYS", "14"))*86400

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
        keys: dict[str, list[int]] = {}
        for p in room.game.players:
            if not p.owner_id and not p.model_key.startswith("mock:"):
                keys.setdefault(p.model_key, []).append(p.id)
        data["shared_models"] = {key: seats for key, seats in keys.items() if len(seats)>1}
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
                    packet = {k: deepcopy(event[k]) for k in ("type", "data", "game_id", "turn_id", "turn_sequence", "state_revision", "event_id", "seq")}
                    self.push(connection, packet)
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

    async def create(self, owner_id: str, name: str, seat: int, title: str, pace: str, action_id: str | None = None) -> dict[str, Any]:
        existing = await self.store.call("created", owner_id, action_id) if action_id else None
        if existing:
            return self.snapshot(await self.get_async(existing.room_id), owner_id)
        await asyncio.to_thread(self.limits.check, "room", owner_id)
        game = WerewolfGame(secrets.token_hex(5), owner_id, title=title, pace=pace, creation_action_id=action_id)
        room = self._room(game)
        room.engine.join(owner_id, name, seat)
        log.info("room_created room_id=%s request_id=%s", game.room_id, action_id)
        self.rooms[game.room_id] = room
        await self.commit_async(room)
        return self.snapshot(room, owner_id)

    async def join(self, room_id: str, owner_id: str, name: str, seat: int, password: str = "") -> dict[str, Any]:
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
                    return {"left": True, "room_id": room_id, "action_ack": {**room.game.processed_actions[cache_key], "replayed": True}}
                return self.acknowledgement(room, owner_id, room.game.processed_actions[cache_key], True)
            if not room.game.owned_player(owner_id):
                raise PermissionError("你不属于此房间")
            await self.rate("chat" if command == "wolf_chat" else "action", room_id, owner_id)
            if room.game.lifecycle in {"ARCHIVED", "DELETED"}:
                raise ValueError("房间已关闭，只能查看历史")
            if room.engine.tick():
                await self.commit_async(room)
            if command != "action":
                if payload.get("game_id") and payload["game_id"] != room.game.game_id:
                    raise ValueError("对局已变更，请同步")
                if payload.get("expected_state_revision") is not None and payload["expected_state_revision"] != room.game.state_revision:
                    raise ValueError("状态版本已变更，请同步后重试")
            if command == "action":
                p = room.game.owned_player(owner_id)
                room.engine.apply(p.id, payload)
                if room.game.pets[owner_id].delegate_next:
                    room.engine.consume_delegate(owner_id)
            elif command == "start":
                self.limits.check_active_rooms(sum(r.game.lifecycle == "ACTIVE" for r in self.rooms.values()))
                presets = [{"id": seat, "provider": "mock", "personality": list(PERSONALITIES)[seat-1], **room.game.seat_presets.get(str(seat), {})} for seat in range(1,7) if not any(p.id==seat for p in room.game.players)]
                assignments = self.router.registry.allocate(presets, unique=room.game.unique_model_per_ai_seat)
                room.engine.start(owner_id, model_assignments=assignments)
            elif command in {"lock", "password", "kick", "reopen"}:
                await asyncio.to_thread(room.engine.moderate, owner_id, command, payload)
            elif command == "configure":
                room.engine.configure(owner_id, payload["pace"], payload["seats"], payload.get("unique_model_per_ai_seat"))
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
                room.engine.leave(owner_id)
                if cache_key:
                    room.game.processed_actions[cache_key] = {"action_id": action_id, "game_id": room.game.game_id, "state_revision": room.game.state_revision}
                    if len(room.game.processed_actions) > 5000:
                        room.game.processed_actions.pop(next(iter(room.game.processed_actions)))
                await self.commit_async(room)
                return {"left": True, "room_id": room_id}
            elif command == "close":
                room.engine.close_room(owner_id)
                for task in room.jobs.values(): task.cancel()
                room.jobs.clear()
            elif command == "rematch":
                room.engine.rematch(owner_id)
                for task in room.jobs.values(): task.cancel()
                room.jobs.clear()
            else:
                raise ValueError("未知命令")
            stored = {"action_id": action_id, "game_id": room.game.game_id, "state_revision": room.game.state_revision}
            if cache_key:
                room.game.processed_actions[cache_key] = stored
                if len(room.game.processed_actions)>5000:
                    room.game.processed_actions.pop(next(iter(room.game.processed_actions)))
            await self.commit_async(room)
            return self.acknowledgement(room, owner_id, stored, False) if cache_key else self.snapshot(room, owner_id)

    async def pet_chat(self, room_id: str, owner_id: str, text: str, client_message_id: str | None = None) -> dict[str, Any]:
        room = await self.require_async(room_id, owner_id)
        pet_lock = room.pet_locks.setdefault(owner_id, asyncio.Lock())
        if client_message_id and any(entry.get("client_message_id")==client_message_id and entry["role"]=="assistant" for entry in room.game.pets[owner_id].private_chat_history):
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
                with self.router.request_scope(room_id=room_id, game_id=view["game_id"], agent_id=pet["agent_id"], category="pet_chat", day=view["day"], phase=view["phase"], request_id=client_message_id, guard=self.limits):
                    reply, memory = await asyncio.wait_for(self.ai.pet_reply(view, pet, text, await self.store.call("memory", owner_id)), timeout=min(self.router.timeout*3+1, 45))
            except Exception as exc:
                log.warning("Pet provider unavailable: %s", type(exc).__name__)
                reply = "搭档暂时无法连接模型。建议先核对公开发言与票型，避免把猜测当成事实。"
                memory = memory_from(view)
            async with room.lock:
                if room.game.game_id != view["game_id"] or room.game.lifecycle == "ARCHIVED":
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
            self.push(connection, {"type": "state_snapshot", "data": self.snapshot(room, owner_id), "sync": {"mode": "authoritative_snapshot", "last_event_id": last_event_id}})
            return connection

    def can_control(self, room: Room, pid: int) -> bool:
        p = room.game.player(pid)
        if not p.owner_id:
            return True
        pet = room.game.pets[p.owner_id]
        return pet.control_mode == "autopilot" or pet.delegate_next

    def applicable(self, room: Room, pid: int, view: dict, discussion: bool = False) -> bool:
        g = room.game
        valid = g.game_id==view["game_id"] and g.turn_id==view["turn_id"] and g.lifecycle=="ACTIVE" and self.can_control(room,pid)
        if valid:
            p = g.player(pid)
            valid = (p.alive and p.role=="wolf" and g.phase=="night_discussion") if discussion else bool(room.engine.action_for(pid))
        if not valid:
            log.info("stale_ai_response room=%s game=%s turn=%s revision=%s", g.room_id, view["game_id"], view["turn_id"], view["state_revision"])
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
                p.conversation_state["belief_state"] = GameBeliefState.from_view(view, memory).dump()
                if p.owner_id:
                    pet = room.game.pets[p.owner_id]
                    provider, personality, style, agent_id = pet.model_key or pet.provider, pet.personality, dict(pet.play_style), pet.agent_id
                else:
                    provider, personality, style, agent_id = p.model_key or p.provider, p.personality, {}, p.agent_id
            category = "wolf_discussion" if discussion else view["pending_action"]["type"]
            with self.router.request_scope(room_id=room.game.room_id, game_id=view["game_id"], agent_id=agent_id, category=category, day=view["day"], phase=view["phase"], request_id=f"{view['turn_id']}:{pid}", guard=self.limits):
                if discussion:
                    message = await self.ai.wolf_discuss(view, provider, personality, memory, style)
                    async with room.lock:
                        if not self.applicable(room,pid,view,True): return
                        room.engine.wolf_message(pid, message)
                        self.execution(room,pid,agent_id,category)
                        await self.commit_async(room)
                    return
                if category == "speech":
                    async for chunk in self.ai.speak(view, provider, personality, memory, style):
                        async with room.lock:
                            if not self.applicable(room,pid,view) or not room.engine.append_speech(pid,chunk,sequence): return
                            # Hot path: event only, no full state serialization or SQLite write.
                            await self.commit_async(room, save=False, snapshot=False)
                    await asyncio.sleep(self.ai_pause)
                    async with room.lock:
                        if not self.applicable(room,pid,view): return
                        payload = {"action":"speech", "speech":room.game.current_speech or "我先听后续发言。", "game_id":view["game_id"], "turn_sequence":sequence, "turn_id":view["turn_id"]}
                        execution = self.execution(room,pid,agent_id,category)
                        if execution.get("status") == "partial":
                            room.engine.emit("speech_error", {"player_id":pid,"partial":True,"message":"模型输出中断，已保留文字"})
                        room.engine.apply(pid,payload)
                        if p.owner_id: room.engine.consume_delegate(p.owner_id)
                        await self.commit_async(room)
                else:
                    proposal = await self.ai.propose(view,provider,personality,memory,style)
                    await asyncio.sleep(min(self.ai_pause,0.7))
                    async with room.lock:
                        if not self.applicable(room,pid,view): return
                        room.engine.apply(pid,proposal)
                        self.execution(room,pid,agent_id,category)
                        if p.owner_id: room.engine.consume_delegate(p.owner_id)
                        await self.commit_async(room)
        except asyncio.CancelledError:
            if view:
                log.info("stale_ai_response cancelled room=%s turn=%s", room.game.room_id, view["turn_id"])
            raise
        except ValueError:
            if view: log.info("stale_ai_response rejected room=%s turn=%s", room.game.room_id,view["turn_id"])
        except Exception as exc:
            log.warning("AI action failed room=%s error=%s; clock continues", room.game.room_id,type(exc).__name__)
            async with room.lock:
                if view and self.applicable(room,pid,view) and room.game.current_speech:
                    room.engine.emit("speech_error", {"player_id":pid,"partial":True,"message":"模型输出中断，已保留文字"})
                    await self.commit_async(room)

    async def discussion_rounds(self, room: Room, sequence: int) -> None:
        # The second wolf sees the first one's completed message; the next round
        # can revise the plan. Human wolf messages may arrive between responses.
        for _ in range(2):
            for p in list(room.game.players):
                if room.game.turn_sequence!=sequence: return
                if p.alive and p.role=="wolf" and self.can_control(room,p.id):
                    await self.actor(room,p.id,sequence,True)

    def launch_jobs(self, room: Room) -> None:
        g = room.game
        for key, task in list(room.jobs.items()):
            if key[0]!=g.turn_sequence:
                if not task.done(): task.cancel()
                room.jobs.pop(key,None)
        if g.lifecycle!="ACTIVE" or g.phase in {"lobby","finished"}: return
        if g.phase=="night_discussion":
            key=(g.turn_sequence,0)
            if key not in room.jobs:
                room.jobs[key]=asyncio.create_task(self.discussion_rounds(room,g.turn_sequence))
            return
        for p in g.players:
            if room.engine.action_for(p.id) and self.can_control(room,p.id):
                key=(g.turn_sequence,p.id)
                if key not in room.jobs:
                    room.jobs[key]=asyncio.create_task(self.actor(room,p.id,g.turn_sequence))

    def maintenance(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        protected = {rid for rid,r in self.rooms.items() if r.connections or r.game.lifecycle=="ACTIVE"}
        archived = self.store.archive_expired(now=now,lobby_seconds=self.lobby_ttl,finished_seconds=self.finished_ttl,exclude=protected)
        for rid in archived:
            if rid in self.rooms:
                restored = self.store.get(rid)
                self.rooms[rid].game = restored
                self.rooms[rid].engine = RuleEngine(restored, time_scale=self.time_scale)
        purged = self.store.cleanup_expired_rooms(now=now, exclude={rid for rid in protected if self.rooms[rid].game.lifecycle != "ARCHIVED"})
        self.store.cleanup_expired_sessions(now=now)
        for rid in purged:
            expired = self.rooms.pop(rid, None)
            if expired:
                for connection in expired.connections:
                    self.push(connection, {"type": "room_left", "data": {"room_id": rid}})
                    self.push(connection, {"type": "reconnect", "data": {}})
        for rid,room in list(self.rooms.items()):
            if room.game.lifecycle!="ACTIVE" and not room.connections and now-room.last_access>=self.unload_seconds and not room.lock.locked():
                if rid not in archived: self.store.save(room.game)
                self.rooms.pop(rid,None)

    async def maintenance_async(self) -> None:
        now = time.time()
        # Hold loaded idle rooms during archival; state mutations stay on the loop.
        locked = []
        try:
            for room in list(self.rooms.values()):
                if room.game.lifecycle != "ACTIVE" and not room.lock.locked():
                    await room.lock.acquire()
                    locked.append(room)
            protected = {rid for rid, r in self.rooms.items() if r.connections or r.game.lifecycle == "ACTIVE" or r not in locked}
            archived = await self.store.call("archive_expired", now=now, lobby_seconds=self.lobby_ttl, finished_seconds=self.finished_ttl, exclude=protected)
            for rid in archived:
                if rid in self.rooms:
                    restored = await self.store.call("get", rid)
                    self.rooms[rid].game = restored
                    self.rooms[rid].engine = RuleEngine(restored, time_scale=self.time_scale)
            purged = await self.store.call("cleanup_expired_rooms", now=now, exclude={rid for rid, room in self.rooms.items() if room not in locked})
            await self.store.call("cleanup_expired_sessions", now=now)
            for rid in purged:
                expired = self.rooms.pop(rid, None)
                if expired:
                    for connection in expired.connections:
                        self.push(connection, {"type": "room_left", "data": {"room_id": rid}})
                        self.push(connection, {"type": "reconnect", "data": {}})
            for rid, room in list(self.rooms.items()):
                if room in locked and not room.connections and now-room.last_access >= self.unload_seconds:
                    if rid not in archived:
                        await self.store.call("save", room.game)
                    self.rooms.pop(rid, None)
        finally:
            for room in locked:
                room.lock.release()

    async def run(self) -> None:
        while True:
            for room in list(self.rooms.values()):
                if room.game.lifecycle!="ACTIVE": continue
                try:
                    async with room.lock:
                        changed = room.engine.tick()
                        now=time.time()
                        if changed: await self.commit_async(room)
                        elif room.connections and now-room.last_sync>=5:
                            room.engine.emit("timer_sync", {"server_time":now,"turn_deadline":room.game.turn_deadline},mutation=False)
                            await self.commit_async(room,save=False,snapshot=False)
                            room.last_sync=now
                        self.launch_jobs(room)
                except Exception as exc:
                    log.error("Room clock error room=%s error=%s",room.game.room_id,type(exc).__name__)
            if time.time()-self.last_maintenance>=30:
                # Maintenance runs serially between scheduler ticks; storage IO leaves the loop.
                await self.maintenance_async()
                self.last_maintenance=time.time()
            await asyncio.sleep(min(0.2,max(0.005,self.time_scale*0.2)))

    async def close(self) -> None:
        tasks=[task for room in self.rooms.values() for task in room.jobs.values()]
        if self.runner: tasks.append(self.runner)
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        for room in self.rooms.values(): await self.store.call("save", room.game, room.engine.outbox)
        await self.router.close()

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from anyio import CancelScope
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .game import PERSONALITIES
from .llm import LLMRouter
from .limits import RateLimitError
from .persistence import Store
from .rooms import RoomManager
from .runtime_lock import RuntimeLock

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR.parent / ".env")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_path = os.getenv("DATABASE_PATH", str(BASE_DIR.parent / "data" / "werewolf.sqlite3"))
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # Multiple schedulers against SQLite would advance a room twice. Fail explicitly.
    lock_file = RuntimeLock(db_path + ".lock")
    store = Store(db_path)
    router = LLMRouter()
    scale = float(os.getenv("GAME_TIME_SCALE", "1"))
    if scale <= 0: raise RuntimeError("GAME_TIME_SCALE must be positive")
    manager = RoomManager(store, router, time_scale=scale, ai_pause=max(0, float(os.getenv("AI_TURN_PAUSE", "1.5"))))
    app.state.manager = manager
    manager.runner = asyncio.create_task(manager.run())
    try:
        yield
    finally:
        await manager.close()
        store.close()
        lock_file.close()


app = FastAPI(title="AI Werewolf", version="2.3.0", lifespan=lifespan)
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'; worker-src 'self'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def manager() -> RoomManager:
    return app.state.manager


async def owner(authorization: str | None = Header(default=None)) -> str:
    token = authorization.removeprefix("Bearer ") if authorization and authorization.startswith("Bearer ") else ""
    identity = await manager().store.call("authenticate", token)
    if not identity: raise HTTPException(401, "请创建或恢复你的登录会话")
    return identity


class CommandRequest(BaseModel):
    action_id: str | None = Field(default=None, min_length=8, max_length=80)
    expected_state_revision: int | None = Field(default=None, ge=0)
    turn_id: str | None = Field(default=None, max_length=100)
    game_id: str | None = Field(default=None, max_length=64)


class RoomRequest(CommandRequest):
    name: str = Field(default="玩家", min_length=1, max_length=24)
    seat: int = Field(default=1, ge=1, le=6)
    title: str = Field(default="月下狼人杀", min_length=1, max_length=40)
    pace: Literal["fast", "standard", "slow"] = "standard"


class JoinRequest(CommandRequest):
    password: str = Field(default="", max_length=64)
    name: str = Field(default="玩家", min_length=1, max_length=24)
    seat: int = Field(default=1, ge=1, le=6)


class ConfigureRequest(CommandRequest):
    pace: Literal["fast", "standard", "slow"] = "standard"
    seats: list[dict[str, Any]] = Field(default_factory=list, max_length=6)
    unique_model_per_ai_seat: bool = Field(default=True, strict=True)


class ActionRequest(CommandRequest):
    game_id: str = Field(max_length=64)
    turn_sequence: int
    action: Literal["speech", "vote", "wolf_kill", "seer_inspect", "witch"]
    target: int | None = Field(default=None, strict=True)
    speech: str = Field(default="", max_length=500)
    save: bool = Field(default=False, strict=True)
    poison_target: int | None = Field(default=None, strict=True)


class ChatRequest(CommandRequest):
    text: str = Field(min_length=1, max_length=500)
    client_message_id: str | None = Field(default=None, min_length=8, max_length=80)


class PetConfigRequest(CommandRequest):
    name: str | None = Field(default=None, min_length=1, max_length=24)
    provider: Literal["mock", "openai", "anthropic", "gemini", "dashscope", "auto"] | None = None
    model_key: str | None = Field(default=None, max_length=180)
    personality: str | None = None
    control_mode: Literal["copilot", "consult", "autopilot"] | None = None
    delegate_next: bool | None = None
    play_style: dict[str, Any] | None = None


async def command(room_id: str, identity: str, kind: str, payload: dict[str, Any]):
    try:
        return await manager().command(room_id, identity, kind, payload)
    except PermissionError as exc: raise HTTPException(403, str(exc)) from exc
    except ValueError as exc: raise HTTPException(400, str(exc)) from exc


@app.exception_handler(RateLimitError)
async def rate_limited(request: Request, exc: RateLimitError):
    return JSONResponse(status_code=429, content={"detail": str(exc), "reason": exc.reason}, headers={"Retry-After": str(exc.retry_after)})


@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(BASE_DIR / "static" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.post("/api/session")
async def session(request: Request):
    await asyncio.to_thread(manager().limits.check, "session", request.client.host if request.client else "unknown")
    identity, token = await manager().store.call("identity")
    recovery = await manager().store.call("recovery_code", identity)
    return {"token": token, "owner_id": identity, "recovery_code": recovery}


@app.get("/api/rooms")
async def rooms(identity: str = Depends(owner)):
    return await manager().store.call("listing", identity)


@app.post("/api/rooms")
async def create_room(req: RoomRequest, identity: str = Depends(owner)):
    return await manager().create(identity, req.name, req.seat, req.title, req.pace, req.action_id)


@app.post("/api/rooms/{room_id}/join")
async def join_room(room_id: str, req: JoinRequest, identity: str = Depends(owner)):
    try:
        return await manager().join(room_id, identity, req.name, req.seat, req.password)
    except ValueError as exc: raise HTTPException(400, str(exc)) from exc
    except PermissionError as exc: raise HTTPException(403, str(exc)) from exc


@app.get("/api/rooms/{room_id}")
async def get_room(room_id: str, identity: str = Depends(owner)):
    try:
        room = await manager().require_async(room_id, identity)
        async with room.lock:
            return manager().snapshot(room, identity)
    except PermissionError as exc: raise HTTPException(403, str(exc)) from exc
    except ValueError as exc: raise HTTPException(404, str(exc)) from exc


@app.post("/api/rooms/{room_id}/start")
async def start(room_id: str, req: CommandRequest = CommandRequest(), identity: str = Depends(owner)):
    return await command(room_id, identity, "start", req.model_dump(exclude_none=True))


@app.post("/api/rooms/{room_id}/rematch")
async def rematch(room_id: str, req: CommandRequest = CommandRequest(), identity: str = Depends(owner)):
    return await command(room_id, identity, "rematch", req.model_dump(exclude_none=True))


@app.post("/api/rooms/{room_id}/close")
async def close_room(room_id: str, req: CommandRequest = CommandRequest(), identity: str = Depends(owner)):
    return await command(room_id, identity, "close", req.model_dump(exclude_none=True))


@app.post("/api/rooms/{room_id}/leave")
async def leave_room(room_id: str, req: CommandRequest = CommandRequest(), identity: str = Depends(owner)):
    return await command(room_id, identity, "leave", req.model_dump(exclude_none=True))


@app.post("/api/rooms/{room_id}/configure")
async def configure(room_id: str, req: ConfigureRequest, identity: str = Depends(owner)):
    return await command(room_id, identity, "configure", req.model_dump())


@app.post("/api/rooms/{room_id}/action")
async def action(room_id: str, req: ActionRequest, identity: str = Depends(owner)):
    return await command(room_id, identity, "action", req.model_dump())


@app.post("/api/rooms/{room_id}/wolf-chat")
async def wolf_chat(room_id: str, req: ChatRequest, identity: str = Depends(owner)):
    return await command(room_id, identity, "wolf_chat", req.model_dump())


@app.patch("/api/rooms/{room_id}/pet")
async def pet_config(room_id: str, req: PetConfigRequest, identity: str = Depends(owner)):
    return await command(room_id, identity, "pet_config", req.model_dump(exclude_none=True))


@app.post("/api/rooms/{room_id}/pet/chat")
async def pet_chat(room_id: str, req: ChatRequest, identity: str = Depends(owner)):
    try:
        return await manager().pet_chat(room_id, identity, req.text, req.client_message_id)
    except PermissionError as exc: raise HTTPException(403, str(exc)) from exc
    except ValueError as exc: raise HTTPException(400, str(exc)) from exc


@app.get("/api/pet/memory")
async def pet_memory(identity: str = Depends(owner)):
    return await manager().store.call("memory", identity)


class PreferencesRequest(BaseModel):
    advice_length: Literal["short", "detailed"] = "short"
    strategy: str = Field(default="", max_length=200)


@app.put("/api/pet/memory")
async def preferences(req: PreferencesRequest, identity: str = Depends(owner)):
    await manager().store.call("save_preferences", identity, req.model_dump())
    return await manager().store.call("memory", identity)


@app.post("/api/game/reveal")
async def legacy_reveal():
    raise HTTPException(403, "V2 仅在对局结束后公开身份")


@app.get("/api/health")
async def health():
    return {"ok": True, "version": "2.3.0", "providers": manager().router.status(), "model_registry": manager().router.model_status(), "storage": "sqlite", "personalities": PERSONALITIES}


@app.websocket("/ws/{room_id}")
async def websocket(websocket: WebSocket, room_id: str):
    await websocket.accept()
    connection = None
    sender = None
    received = None
    try:
        # Authenticate in the first frame; tokens never appear in URL/access logs.
        auth = await asyncio.wait_for(websocket.receive_json(), timeout=8)
        session_token = auth.get("token", "") if isinstance(auth, dict) else ""
        identity = await manager().store.call("authenticate", session_token)
        if not identity:
            await websocket.close(code=4401)
            return
        last_event_id = auth.get("last_event_id") if isinstance(auth.get("last_event_id"), str) and len(auth["last_event_id"]) <= 100 else None
        connection = await manager().subscribe(room_id, identity, last_event_id)
        async def send():
            while True:
                try:
                    packet = await asyncio.wait_for(connection.queue.get(), timeout=20)
                except asyncio.TimeoutError:
                    packet = None
                if not await manager().store.call("authenticate", session_token) or packet and packet["type"] == "session_revoked":
                    await websocket.close(code=4401)
                    return
                if packet is None:
                    continue
                if packet["type"] == "reconnect":
                    await websocket.close(code=1013)
                    return
                await asyncio.wait_for(websocket.send_json(packet), timeout=5)
        sender = asyncio.create_task(send())
        # Consume heartbeats and disconnects; gameplay actions use validated HTTP commands.
        while True:
            received = asyncio.create_task(websocket.receive_text())
            done, _ = await asyncio.wait({received, sender}, return_when=asyncio.FIRST_COMPLETED)
            if sender in done:
                received.cancel()
                await asyncio.gather(received, return_exceptions=True)
                break
            received.result()
    except asyncio.CancelledError:
        # Browser disconnect / ASGI shutdown can cancel this endpoint while it
        # waits for either child. Cleanup below must finish in a cancelled scope.
        pass
    except RateLimitError:
        await websocket.close(code=4408)
    except (WebSocketDisconnect, asyncio.TimeoutError, ValueError, PermissionError, RuntimeError):
        try: await websocket.close(code=4403)
        except RuntimeError: pass
    finally:
        pending = [task for task in (sender, received) if task is not None]
        for task in pending:
            task.cancel()
        with CancelScope(shield=True):
            await asyncio.gather(*pending, return_exceptions=True)
        if connection:
            room = manager().rooms.get(room_id)
            if room: room.connections.discard(connection)


class RecoveryRequest(BaseModel):
    code: str = Field(min_length=10, max_length=40)


@app.post("/api/session/recover")
async def recover(req: RecoveryRequest, request: Request):
    await asyncio.to_thread(manager().limits.check, "recovery", request.client.host if request.client else "unknown")
    try:
        restored = await manager().store.call("recover", req.code)
        manager().disconnect_identity(restored["owner_id"])
        return restored
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/session/rotate")
async def rotate(identity: str = Depends(owner)):
    token = await manager().store.call("rotate", identity)
    manager().disconnect_identity(identity)
    return {"token": token, "owner_id": identity}


@app.post("/api/session/recovery-code")
async def recovery_code(identity: str = Depends(owner)):
    return {"recovery_code": await manager().store.call("recovery_code", identity)}


@app.post("/api/session/revoke")
async def revoke(identity: str = Depends(owner)):
    await manager().store.call("revoke", identity)
    manager().disconnect_identity(identity)
    return {"revoked": True}


class LockRequest(CommandRequest):
    locked: bool = Field(strict=True)


class PasswordRequest(CommandRequest):
    password: str = Field(default="", max_length=64)


class SeatRequest(CommandRequest):
    seat: int = Field(ge=1, le=6)


@app.post("/api/rooms/{room_id}/lock")
async def lock_room(room_id: str, req: LockRequest, identity: str = Depends(owner)):
    return await command(room_id, identity, "lock", req.model_dump(exclude_none=True))


@app.post("/api/rooms/{room_id}/password")
async def room_password(room_id: str, req: PasswordRequest, identity: str = Depends(owner)):
    return await command(room_id, identity, "password", req.model_dump(exclude_none=True))


@app.post("/api/rooms/{room_id}/kick")
async def kick(room_id: str, req: SeatRequest, identity: str = Depends(owner)):
    return await command(room_id, identity, "kick", req.model_dump(exclude_none=True))


@app.post("/api/rooms/{room_id}/reopen")
async def reopen(room_id: str, req: SeatRequest, identity: str = Depends(owner)):
    return await command(room_id, identity, "reopen", req.model_dump(exclude_none=True))


@app.get("/api/rooms/{room_id}/cost-report")
async def cost_report(room_id: str, identity: str = Depends(owner)):
    try:
        room = await manager().require_async(room_id, identity)
        if room.game.host_id != identity:
            raise PermissionError("只有房主可以查看成本报告")
        if room.game.phase != "lobby" and not room.game.game_over:
            # Skill timing and agent IDs in a detailed report can reveal roles.
            return {"room_id": room_id, "budget": manager().limits.snapshot(room_id, room.game.game_id), "details_available": False}
        if manager().router._background_writes:
            await asyncio.gather(*manager().router._background_writes, return_exceptions=True)
        stored = await manager().store.call("model_report", room_id, room.game.game_id)
        return {**manager().router.cost_report(room_id, room.game.game_id, stored), "details_available": True}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc

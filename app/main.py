from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from anyio import CancelScope
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .credentials import CredentialService
from .game import GAME_MODES, PERSONALITIES
from .performance import public_profiles
from .limits import RateLimitError
from .llm import LLMRouter
from .persistence import Store
from .roles import ROLE_DEFINITIONS
from .rooms import RoomManager
from .runtime_lock import RuntimeLock
from .trace import research_export
from .versions import APP_VERSION

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
    credentials = CredentialService(db_path + ".credentials.sqlite3")
    router.credentials = credentials
    scale = float(os.getenv("GAME_TIME_SCALE", "1"))
    if scale <= 0:
        raise RuntimeError("GAME_TIME_SCALE must be positive")
    manager = RoomManager(store, router, time_scale=scale, ai_pause=max(0, float(os.getenv("AI_TURN_PAUSE", "1.5"))))
    app.state.manager = manager
    manager.runner = asyncio.create_task(manager.run())
    try:
        yield
    finally:
        await manager.close()
        credentials.close()
        store.close()
        lock_file.close()


app = FastAPI(title="AI Werewolf Arena", version=APP_VERSION, lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError):
    # Pydantic's default errors include the submitted input, including API keys.
    errors = [{"type": error["type"], "loc": error["loc"], "msg": error["msg"]} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'; worker-src 'self'"
    )
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
    if not identity:
        raise HTTPException(401, "请创建或恢复你的登录会话")
    return identity


class CommandRequest(BaseModel):
    action_id: str | None = Field(default=None, min_length=8, max_length=80)
    expected_state_revision: int | None = Field(default=None, ge=0)
    turn_id: str | None = Field(default=None, max_length=100)
    game_id: str | None = Field(default=None, max_length=64)


class RoomRequest(CommandRequest):
    name: str = Field(default="玩家", min_length=1, max_length=24)
    seat: int | None = Field(default=None, ge=1, le=16)
    title: str = Field(default="月下狼人杀", min_length=1, max_length=40)
    pace: Literal["fast", "standard", "slow"] = "standard"
    mode: Literal[
        "quick6",
        "standard9",
        "standard12",
        "wolfking12",
        "beautyknight12",
        "hidden12",
        "advanced9",
        "fun6",
        "special12",
        "custom",
    ] = "quick6"
    player_count: int | None = Field(default=None, ge=4, le=16)
    roles: list[str] | None = Field(default=None, min_length=4, max_length=16)
    board_policy: Literal["fixed", "constrained_random", "custom_random"] | None = None
    random_role_pool: list[str] | None = Field(default=None, max_length=20)


class JoinRequest(CommandRequest):
    password: str = Field(default="", max_length=64)
    name: str = Field(default="玩家", min_length=1, max_length=24)
    seat: int | None = Field(default=None, ge=1, le=16)


class ConfigureRequest(CommandRequest):
    pace: Literal["fast", "standard", "slow"] = "standard"
    seats: list[dict[str, Any]] = Field(default_factory=list, max_length=16)
    unique_model_per_ai_seat: bool = Field(default=True, strict=True)
    ai_performance_profile: Literal["economy", "balanced", "unrestricted", "custom"] | None = None
    ai_performance_custom: dict[str, Any] | None = None
    mode: (
        Literal[
            "quick6",
            "standard9",
            "standard12",
            "wolfking12",
            "beautyknight12",
            "hidden12",
            "advanced9",
            "fun6",
            "special12",
            "custom",
        ]
        | None
    ) = None
    player_count: int | None = Field(default=None, ge=4, le=16)
    roles: list[str] | None = Field(default=None, min_length=4, max_length=16)
    board_policy: Literal["fixed", "constrained_random", "custom_random"] | None = None
    random_role_pool: list[str] | None = Field(default=None, max_length=20)


class ActionRequest(CommandRequest):
    game_id: str = Field(max_length=64)
    turn_sequence: int
    action: Literal[
        "speech",
        "vote",
        "wolf_kill",
        "seer_inspect",
        "witch",
        "wolf_discuss",
        "guard_protect",
        "dream_visit",
        "grave_inspect",
        "crow_mark",
        "wolf_beauty_charm",
        "hunter_shoot",
        "wolf_king_shoot",
        "duel",
        "self_destruct",
    ]
    target: int | None = Field(default=None, strict=True)
    speech: str = Field(default="", max_length=500)
    text: str = Field(default="", max_length=500)
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
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.exception_handler(RateLimitError)
async def rate_limited(request: Request, exc: RateLimitError):
    return JSONResponse(
        status_code=429,
        content={"detail": str(exc), "reason": exc.reason},
        headers={"Retry-After": str(exc.retry_after)},
    )


@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(BASE_DIR / "static" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        BASE_DIR / "static" / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"}
    )


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
    try:
        return await manager().create(
            identity,
            req.name,
            req.seat,
            req.title,
            req.pace,
            req.action_id,
            mode=req.mode,
            player_count=req.player_count,
            roles=req.roles,
            board_policy=req.board_policy or "fixed",
            random_role_pool=req.random_role_pool,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/rooms/{room_id}/join")
async def join_room(room_id: str, req: JoinRequest, identity: str = Depends(owner)):
    try:
        return await manager().join(room_id, identity, req.name, req.seat, req.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.get("/api/rooms/{room_id}")
async def get_room(room_id: str, identity: str = Depends(owner)):
    try:
        room = await manager().require_async(room_id, identity)
        async with room.lock:
            return manager().snapshot(room, identity)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


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


@app.post("/api/rooms/{room_id}/pause")
async def pause_room(room_id: str, req: CommandRequest = CommandRequest(), identity: str = Depends(owner)):
    return await command(room_id, identity, "pause", req.model_dump(exclude_none=True))


@app.post("/api/rooms/{room_id}/resume")
async def resume_room(room_id: str, req: CommandRequest = CommandRequest(), identity: str = Depends(owner)):
    return await command(room_id, identity, "resume", req.model_dump(exclude_none=True))


@app.delete("/api/rooms/{room_id}")
async def delete_room(room_id: str, identity: str = Depends(owner)):
    return await command(room_id, identity, "delete", {})


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
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


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
    return {
        "ok": True,
        "version": APP_VERSION,
        "providers": manager().router.status(),
        "model_registry": manager().router.model_status(),
        "storage": "sqlite",
        "personalities": PERSONALITIES,
        "ai_performance_profiles": public_profiles(),
        "game_modes": GAME_MODES,
        "role_catalog": [role.public() for role in ROLE_DEFINITIONS.values()],
    }


@app.get("/api/models")
async def platform_models(identity: str = Depends(owner)):
    return {"models": manager().router.model_status()}


@app.get("/api/models/preferences")
async def model_preferences(identity: str = Depends(owner)):
    return await manager().store.call("model_preferences", identity)


@app.put("/api/models/preferences")
async def save_model_preferences(req: dict[str, Any], identity: str = Depends(owner)):
    try:
        return await manager().store.call("save_model_preferences", identity, req)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/models/refresh")
async def platform_models_refresh(identity: str = Depends(owner)):
    await asyncio.to_thread(manager().limits.check, "model_test", "platform-catalog")
    discovery = await manager().router.refresh_platform_models()
    return {"models": manager().router.model_status(), "providers": discovery}


class CredentialRequest(BaseModel):
    provider: Literal["openai", "anthropic", "gemini", "dashscope", "openai-compatible"]
    api_key: str = Field(min_length=1, max_length=4096, repr=False)
    base_url: str | None = Field(default=None, max_length=2048)
    temporary: bool = False
    scope_id: str | None = Field(default=None, max_length=100)


class ReplaceCredentialRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=4096, repr=False)
    base_url: str | None = Field(default=None, max_length=2048)


def credential_error(exc: Exception) -> HTTPException:
    # Credential operations use curated error strings, never provider bodies.
    if isinstance(exc, PermissionError):
        return HTTPException(403, "无法访问此凭据")
    return HTTPException(getattr(exc, "status_code", 400), str(exc))


@app.get("/api/credentials")
async def credentials_list(scope_id: str | None = None, identity: str = Depends(owner)):
    return {"credentials": await asyncio.to_thread(manager().router.credentials.list, identity, scope_id=scope_id)}


@app.post("/api/credentials")
async def credentials_create(req: CredentialRequest, identity: str = Depends(owner)):
    await asyncio.to_thread(manager().limits.check, "action", "credential:" + identity)
    try:
        return await asyncio.to_thread(
            manager().router.credentials.create,
            identity,
            req.provider,
            req.api_key,
            base_url=req.base_url,
            temporary=req.temporary,
            scope_id=req.scope_id or ("session:" + identity if req.temporary else None),
        )
    except (ValueError, PermissionError) as exc:
        raise credential_error(exc) from exc


@app.get("/api/credentials/{credential_id}")
async def credentials_get(credential_id: str, scope_id: str | None = None, identity: str = Depends(owner)):
    try:
        return await asyncio.to_thread(manager().router.credentials.get, identity, credential_id, scope_id=scope_id)
    except (ValueError, PermissionError) as exc:
        raise credential_error(exc) from exc


@app.put("/api/credentials/{credential_id}")
async def credentials_replace(
    credential_id: str, req: ReplaceCredentialRequest, scope_id: str | None = None, identity: str = Depends(owner)
):
    try:
        result = await asyncio.to_thread(
            manager().router.credentials.replace,
            identity,
            credential_id,
            req.api_key,
            base_url=req.base_url,
            scope_id=scope_id,
        )
        manager().invalidate_credential(identity, credential_id)
        return result
    except (ValueError, PermissionError) as exc:
        raise credential_error(exc) from exc


@app.delete("/api/credentials/{credential_id}")
async def credentials_delete(credential_id: str, scope_id: str | None = None, identity: str = Depends(owner)):
    try:
        await asyncio.to_thread(manager().router.credentials.delete, identity, credential_id, scope_id=scope_id)
        manager().invalidate_credential(identity, credential_id)
        return {"deleted": True}
    except (ValueError, PermissionError) as exc:
        raise credential_error(exc) from exc


@app.post("/api/credentials/{credential_id}/models")
async def credentials_discover(credential_id: str, scope_id: str | None = None, identity: str = Depends(owner)):
    await asyncio.to_thread(manager().limits.check, "model_test", "credential:" + identity)
    try:
        return await manager().router.credentials.discover(identity, credential_id, scope_id=scope_id)
    except (ValueError, PermissionError) as exc:
        raise credential_error(exc) from exc


class CredentialTestRequest(BaseModel):
    model_id: str | None = Field(default=None, max_length=200)


@app.post("/api/credentials/{credential_id}/test")
async def credentials_test(
    credential_id: str,
    req: CredentialTestRequest = CredentialTestRequest(),
    scope_id: str | None = None,
    identity: str = Depends(owner),
):
    await asyncio.to_thread(manager().limits.check, "model_test", "credential:" + identity)
    try:
        return await manager().router.credentials.test(
            identity, credential_id, scope_id=scope_id, model_id=req.model_id
        )
    except (ValueError, PermissionError) as exc:
        raise credential_error(exc) from exc


@app.get("/api/rooms/{room_id}/analysis")
async def analysis(room_id: str, game_id: str | None = None, identity: str = Depends(owner)):
    try:
        return await manager().analysis(room_id, identity, game_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/history")
async def user_history(identity: str = Depends(owner)):
    return {"games": await manager().store.call("user_history", identity)}


@app.get("/api/history/{room_id}/{game_id}")
async def historical_analysis(room_id: str, game_id: str, identity: str = Depends(owner)):
    try:
        return await manager().analysis(room_id, identity, game_id, historical_only=True)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/rooms/{room_id}/replay")
async def replay(room_id: str, game_id: str | None = None, identity: str = Depends(owner)):
    report = await analysis(room_id, game_id, identity)
    return {key: report[key] for key in ("game_id", "winner", "events")}


@app.get("/api/rooms/{room_id}/games")
async def games(room_id: str, identity: str = Depends(owner)):
    try:
        await manager().require_async(room_id, identity)
        return {"games": await manager().store.call("completed_list", room_id)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/rooms/{room_id}/games/{game_id}/export")
async def export_game(room_id: str, game_id: str, identity: str = Depends(owner)):
    # Historical host ownership is independent of current membership/replay.
    game = await manager().store.call("completed_game", room_id, game_id)
    if game is None:
        raise HTTPException(404, "Completed game not found")
    if game.host_id != identity:
        raise HTTPException(403, "Research export requires the original host")
    events = await manager().store.call("research_events", room_id, game_id)
    telemetry = await manager().store.call("model_report", room_id, game_id)
    return StreamingResponse(
        research_export(game, events, telemetry),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/benchmark")
async def benchmark_page():
    return FileResponse(Path(__file__).parent / "static" / "benchmark.html")


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
        last_event_id = (
            auth.get("last_event_id")
            if isinstance(auth.get("last_event_id"), str) and len(auth["last_event_id"]) <= 100
            else None
        )
        connection = await manager().subscribe(room_id, identity, last_event_id)

        async def send():
            while True:
                try:
                    packet = await asyncio.wait_for(connection.queue.get(), timeout=20)
                except asyncio.TimeoutError:
                    packet = None
                if (
                    not await manager().store.call("authenticate", session_token)
                    or packet
                    and packet["type"] == "session_revoked"
                ):
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
        try:
            await websocket.close(code=4403)
        except RuntimeError:
            pass
    finally:
        pending = [task for task in (sender, received) if task is not None]
        for task in pending:
            task.cancel()
        with CancelScope(shield=True):
            await asyncio.gather(*pending, return_exceptions=True)
        if connection:
            with CancelScope(shield=True):
                await manager().unsubscribe(room_id, connection)


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
    manager().router.credentials.clear_scope("session:" + identity)
    for room in manager().rooms.values():
        for player in room.game.players:
            if getattr(player, "credential_owner_id", None) == identity and getattr(player, "credential_id", None):
                manager().invalidate_credential(identity, player.credential_id)
    manager().disconnect_identity(identity)
    return {"revoked": True}


class LockRequest(CommandRequest):
    locked: bool = Field(strict=True)


class PasswordRequest(CommandRequest):
    password: str = Field(default="", max_length=64)


class SeatRequest(CommandRequest):
    seat: int = Field(ge=1, le=16)


class ChangeSeatRequest(CommandRequest):
    seat: int | None = Field(default=None, ge=1, le=16)


@app.post("/api/rooms/{room_id}/seat")
async def change_seat(room_id: str, req: ChangeSeatRequest, identity: str = Depends(owner)):
    return await command(room_id, identity, "seat", req.model_dump(exclude_none=True))


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
            return {
                "room_id": room_id,
                "budget": manager().limits.snapshot(room_id, room.game.game_id),
                "details_available": False,
            }
        if manager().router._background_writes:
            await asyncio.gather(*manager().router._background_writes, return_exceptions=True)
        stored = await manager().store.call("model_report", room_id, room.game.game_id)
        return {**manager().router.cost_report(room_id, room.game.game_id, stored), "details_available": True}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc

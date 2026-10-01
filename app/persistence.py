"""SQLite storage (one scheduler process); storage boundary can be replaced by PG/Redis."""
from __future__ import annotations

import hashlib
import asyncio
from functools import wraps
import threading
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Any

from .game import WerewolfGame


def synchronized(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        with self.lock:
            return fn(self, *args, **kwargs)
    return wrapped


class Store:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS identities (token_hash TEXT PRIMARY KEY, owner_id TEXT UNIQUE NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS rooms (room_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS pet_memory (owner_id TEXT PRIMARY KEY, preferences TEXT NOT NULL DEFAULT '{}', statistics TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS room_events (room_id TEXT NOT NULL, event_id TEXT NOT NULL, game_id TEXT NOT NULL, audience TEXT NOT NULL, player_id INTEGER, event TEXT NOT NULL, PRIMARY KEY(room_id,event_id));
        CREATE TABLE IF NOT EXISTS player_memory (room_id TEXT NOT NULL, game_id TEXT NOT NULL, player_id INTEGER NOT NULL, memory TEXT NOT NULL, PRIMARY KEY(room_id,game_id,player_id));
        CREATE TABLE IF NOT EXISTS room_statistics (room_id TEXT PRIMARY KEY, statistics TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS model_telemetry (record_id TEXT PRIMARY KEY, room_id TEXT, game_id TEXT, kind TEXT NOT NULL, record TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS completed_games (room_id TEXT NOT NULL, game_id TEXT NOT NULL, state TEXT NOT NULL, finished_at REAL NOT NULL, PRIMARY KEY(room_id,game_id));
        CREATE TABLE IF NOT EXISTS usage_state (id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL);
        ''')
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(identities)")}
        if "recovery_hash" not in columns:
            self.db.execute("ALTER TABLE identities ADD COLUMN recovery_hash TEXT")
        if "revoked" not in columns:
            self.db.execute("ALTER TABLE identities ADD COLUMN revoked INTEGER NOT NULL DEFAULT 0")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS identity_recovery ON identities(recovery_hash)")
        self.db.execute("CREATE INDEX IF NOT EXISTS event_room_game ON room_events(room_id,game_id)")
        self.db.execute("CREATE INDEX IF NOT EXISTS telemetry_room_game ON model_telemetry(room_id,game_id)")
        self.db.commit()

    async def call(self, method: str, *args, **kwargs):
        return await asyncio.to_thread(getattr(self, method), *args, **kwargs)

    @synchronized
    def identity(self) -> tuple[str, str]:
        token, owner_id = secrets.token_urlsafe(32), secrets.token_hex(16)
        with self.db:
            self.db.execute("INSERT INTO identities(token_hash, owner_id, created_at) VALUES (?, ?, ?)", (hashlib.sha256(token.encode()).hexdigest(), owner_id, time.time()))
        return owner_id, token

    @synchronized
    def authenticate(self, token: str) -> str | None:
        if not isinstance(token, str) or not token or len(token) > 200:
            return None
        cutoff = time.time() - float(os.getenv("SESSION_TTL_DAYS", "30"))*86400
        row = self.db.execute("SELECT owner_id FROM identities WHERE token_hash = ? AND created_at >= ? AND revoked=0", (hashlib.sha256(token.encode()).hexdigest(), cutoff)).fetchone()
        return row[0] if row else None

    @synchronized
    def recovery_code(self, owner_id: str) -> str:
        code = "-".join(secrets.token_hex(2).upper() for _ in range(5))
        digest = hashlib.sha256(code.replace("-", "").encode()).hexdigest()
        with self.db:
            self.db.execute("UPDATE identities SET recovery_hash=? WHERE owner_id=?", (digest, owner_id))
        return code

    @synchronized
    def rotate(self, owner_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.db:
            changed = self.db.execute("UPDATE identities SET token_hash=?,created_at=?,revoked=0 WHERE owner_id=?", (hashlib.sha256(token.encode()).hexdigest(), time.time(), owner_id)).rowcount
        if not changed:
            raise ValueError("会话已过期，请创建新会话")
        return token

    @synchronized
    def recover(self, code: str) -> dict[str, str]:
        clean = code.replace("-", "").replace(" ", "").upper()
        if len(clean) != 20 or any(c not in "0123456789ABCDEF" for c in clean):
            raise ValueError("恢复码无效或已过期")
        digest = hashlib.sha256(clean.encode()).hexdigest()
        cutoff = time.time() - float(os.getenv("SESSION_TTL_DAYS", "30"))*86400
        row = self.db.execute("SELECT owner_id FROM identities WHERE recovery_hash=? AND created_at>=?", (digest, cutoff)).fetchone()
        if not row:
            raise ValueError("恢复码无效或已过期")
        identity = row[0]
        with self.db:
            token = self.rotate(identity)
            recovery = self.recovery_code(identity)
        return {"owner_id": identity, "token": token, "recovery_code": recovery}

    @synchronized
    def revoke(self, owner_id: str) -> None:
        with self.db:
            self.db.execute("UPDATE identities SET revoked=1 WHERE owner_id=?", (owner_id,))

    @synchronized
    def save(self, g: WerewolfGame, events: list[dict] | None = None) -> None:
        if g.lifecycle == "ARCHIVED" and g.archived_at is None:
            g.archived_at = time.time()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO rooms VALUES (?, ?, ?)", (g.room_id, json.dumps(g.dump(), ensure_ascii=False), time.time()))
            if g.game_over:
                self.db.execute("INSERT OR REPLACE INTO completed_games VALUES (?,?,?,?)", (g.room_id, g.game_id, json.dumps(g.dump(), ensure_ascii=False), g.finished_at or time.time()))
            for event in events or []:
                self.db.execute("INSERT OR IGNORE INTO room_events VALUES(?,?,?,?,?,?)", (g.room_id, event["event_id"], event["game_id"], event["audience"], event.get("player_id"), json.dumps(event, ensure_ascii=False)))
            for player in g.players:
                self.db.execute("INSERT OR REPLACE INTO player_memory VALUES(?,?,?,?)", (g.room_id, g.game_id, player.id, json.dumps(player.memory, ensure_ascii=False)))
            if g.phase == "lobby":
                ids = [p.id for p in g.players]
                self.db.execute("DELETE FROM player_memory WHERE room_id=? AND player_id NOT IN (" + ",".join("?" for _ in ids) + ")", (g.room_id, *ids))

    @synchronized
    def load(self) -> list[WerewolfGame]:
        return [WerewolfGame.restore(json.loads(row[0])) for row in self.db.execute("SELECT state FROM rooms")]

    @synchronized
    def load_active(self) -> list[WerewolfGame]:
        return [WerewolfGame.restore(json.loads(row[0])) for row in self.db.execute("SELECT state FROM rooms WHERE COALESCE(json_extract(state,'$.lifecycle'),CASE WHEN json_extract(state,'$.game_over')=1 THEN 'FINISHED' WHEN json_extract(state,'$.phase')='lobby' THEN 'LOBBY' ELSE 'ACTIVE' END)='ACTIVE'")]

    @synchronized
    def get(self, room_id: str) -> WerewolfGame | None:
        row = self.db.execute("SELECT state FROM rooms WHERE room_id=?", (room_id,)).fetchone()
        return WerewolfGame.restore(json.loads(row[0])) if row else None

    @synchronized
    def created(self, owner_id: str, action_id: str) -> WerewolfGame | None:
        row = self.db.execute("SELECT state FROM rooms WHERE json_extract(state,'$.host_id')=? AND json_extract(state,'$.creation_action_id')=?", (owner_id, action_id)).fetchone()
        return WerewolfGame.restore(json.loads(row[0])) if row else None

    @synchronized
    def listing(self, owner_id: str) -> list[dict[str, Any]]:
        result = []
        for row in self.db.execute("SELECT state FROM rooms WHERE COALESCE(json_extract(state,'$.lifecycle'),'LOBBY') NOT IN ('ARCHIVED','DELETED')"):
            state = json.loads(row[0])
            if any(p.get("owner_id") == owner_id for p in state["players"]):
                result.append({k: state.get(k) for k in ("room_id", "title", "phase", "game_id", "lifecycle")})
        return result

    @synchronized
    def archive_expired(self, *, now: float, lobby_seconds: float, finished_seconds: float, exclude: set[str] | None = None) -> list[str]:
        # Query lifecycle metadata before decoding; no recurring scan of every historical state.
        excluded = sorted(exclude or set())
        query = "SELECT room_id,state FROM rooms WHERE ((COALESCE(json_extract(state,'$.lifecycle'),'LOBBY')='LOBBY' AND updated_at<?) OR (json_extract(state,'$.lifecycle')='FINISHED' AND COALESCE(json_extract(state,'$.finished_at'),updated_at)<?))"
        if excluded:
            query += " AND room_id NOT IN ("+",".join("?" for _ in excluded)+")"
        rows = self.db.execute(query, (now-lobby_seconds, now-finished_seconds, *excluded)).fetchall()
        archived = []
        for room_id, encoded in rows:
            g = WerewolfGame.restore(json.loads(encoded))
            g.lifecycle = "ARCHIVED"
            g.archived_at = now
            g.state_revision += 1
            self.save(g)
            archived.append(room_id)
        with self.db:
            self.db.execute("DELETE FROM identities WHERE created_at<?", (now-float(os.getenv("SESSION_TTL_DAYS", "30"))*86400,))
        return archived

    @synchronized
    def cleanup_expired_rooms(self, *, now: float, retention_days: float | None = None, exclude: set[str] | None = None) -> list[str]:
        retention = retention_days if retention_days is not None else float(os.getenv("ROOM_ARCHIVED_RETENTION_DAYS", "14"))
        rows = self.db.execute("SELECT room_id,state FROM rooms WHERE json_extract(state,'$.lifecycle')='ARCHIVED' AND COALESCE(json_extract(state,'$.archived_at'),updated_at)<?", (now-max(1, retention)*86400,)).fetchall()
        purged = []
        with self.db:
            for rid, encoded in rows:
                if rid in (exclude or set()):
                    continue
                data = json.loads(encoded)
                # Only anonymous aggregates survive: no title, nicknames, IDs, chat or roles.
                stats = {"days": data.get("day", 1), "winner": data.get("winner"), "human_count": sum(bool(p.get("owner_id")) for p in data["players"]), "purged_at": now}
                self.db.execute("INSERT OR REPLACE INTO room_statistics VALUES(?,?)", (rid, json.dumps(stats)))
                for table in ("room_events", "player_memory", "model_telemetry", "completed_games", "rooms"):
                    self.db.execute(f"DELETE FROM {table} WHERE room_id=?", (rid,))
                purged.append(rid)
            # Private skill/AI memory of archived rooms shares exactly this retention.
            self.db.execute("DELETE FROM pet_memory WHERE owner_id NOT IN (SELECT owner_id FROM identities) AND owner_id NOT IN (SELECT json_extract(p.value,'$.owner_id') FROM rooms,json_each(rooms.state,'$.players') p)")
        if purged:
            # Also compact old snapshot pages from pre-V2.3 databases and WAL.
            self.db.execute("VACUUM")
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return purged

    @synchronized
    def record_model(self, kind: str, record: dict[str, Any]) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO model_telemetry VALUES(?,?,?,?,?)",
                            (secrets.token_hex(16), record.get("room_id"), record.get("game_id"), kind, json.dumps(record)))

    @synchronized
    def model_report(self, room_id: str, game_id: str) -> dict[str, Any]:
        result = {"calls": [], "outcomes": []}
        for kind, record in self.db.execute("SELECT kind,record FROM model_telemetry WHERE room_id=? AND game_id=? ORDER BY rowid", (room_id, game_id)):
            result["calls" if kind == "call" else "outcomes"].append(json.loads(record))
        return result

    @synchronized
    def completed_game(self, room_id: str, game_id: str) -> WerewolfGame | None:
        row = self.db.execute("SELECT state FROM completed_games WHERE room_id=? AND game_id=?", (room_id, game_id)).fetchone()
        return WerewolfGame.restore(json.loads(row[0])) if row else None

    @synchronized
    def completed_list(self, room_id: str) -> list[dict[str, Any]]:
        return [{"game_id": game_id, "finished_at": finished, "winner": json.loads(state).get("winner")}
                for game_id, finished, state in self.db.execute("SELECT game_id,finished_at,state FROM completed_games WHERE room_id=? ORDER BY finished_at DESC", (room_id,))]

    @synchronized
    def public_replay(self, room_id: str, game_id: str) -> list[dict[str, Any]]:
        return [json.loads(row[0]) for row in self.db.execute("SELECT event FROM room_events WHERE room_id=? AND game_id=? AND audience='public' ORDER BY rowid", (room_id, game_id))]

    @synchronized
    def cleanup_expired_sessions(self, *, now: float) -> int:
        with self.db:
            return self.db.execute("DELETE FROM identities WHERE created_at<?", (now-float(os.getenv("SESSION_TTL_DAYS", "30"))*86400,)).rowcount

    @synchronized
    def usage_load(self) -> dict[str, Any]:
        row = self.db.execute("SELECT state FROM usage_state WHERE id=1").fetchone()
        return json.loads(row[0]) if row else {}

    @synchronized
    def usage_save(self, state: dict[str, Any]) -> None:
        with self.db:
            self.db.execute("INSERT INTO usage_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state", (json.dumps(state),))

    @synchronized
    def memory(self, owner_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT preferences, statistics FROM pet_memory WHERE owner_id = ?", (owner_id,)).fetchone()
        return {"preferences": json.loads(row[0]) if row else {}, "statistics": json.loads(row[1]) if row else {}}

    @synchronized
    def save_preferences(self, owner_id: str, preferences: dict[str, Any]) -> None:
        with self.db:
            self.db.execute("INSERT INTO pet_memory (owner_id, preferences) VALUES (?, ?) ON CONFLICT(owner_id) DO UPDATE SET preferences=excluded.preferences", (owner_id, json.dumps(preferences, ensure_ascii=False)))

    @synchronized
    def close(self) -> None:
        self.db.close()

"""Owner-scoped BYOK vault and provider model discovery.

Secrets cross this boundary only as internal Credential objects. Public methods
return explicit allowlists; provider bodies and exception strings are never sent
back to clients. Temporary credentials are memory-only and scoped to a room.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import sqlite3
import threading
from dataclasses import dataclass, field, replace
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken

PROVIDER_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "dashscope": "https://dashscope.aliyuncs.com",
}
PROVIDERS = {*PROVIDER_URLS, "openai-compatible"}
TRUSTED_HOSTS = {urlsplit(url).hostname for url in PROVIDER_URLS.values()}


class CredentialError(ValueError):
    """A deliberately safe error suitable for an API response."""
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_provider(provider: str) -> str:
    provider = str(provider).strip().lower().replace("_", "-")
    provider = {"compatible": "openai-compatible", "openai-compat": "openai-compatible"}.get(provider, provider)
    if provider not in PROVIDERS:
        raise CredentialError("不支持的模型供应商")
    return provider


def validate_model_id(model_id: str) -> str:
    model_id = str(model_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", model_id) or ".." in model_id:
        raise CredentialError("模型 ID 格式无效")
    return model_id


def validate_model_options(options: dict | None) -> dict:
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise CredentialError("模型参数须为对象")
    allowed = {"temperature", "reasoning_effort", "thinking_budget", "enable_thinking", "max_output_tokens"}
    if set(options) - allowed:
        raise CredentialError("模型参数仅支持 temperature、reasoning_effort、thinking_budget、enable_thinking、max_output_tokens")
    result = dict(options)
    if "temperature" in result:
        value = result["temperature"]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 2:
            raise CredentialError("temperature 必须介于 0 和 2")
    if "reasoning_effort" in result and (not isinstance(result["reasoning_effort"], str) or result["reasoning_effort"] not in {"none", "minimal", "low", "medium", "high", "xhigh"}):
        raise CredentialError("reasoning_effort 无效")
    if "thinking_budget" in result and (type(result["thinking_budget"]) is not int or not 0 <= result["thinking_budget"] <= 32768):
        raise CredentialError("thinking_budget 必须介于 0 和 32768")
    if "enable_thinking" in result and type(result["enable_thinking"]) is not bool:
        raise CredentialError("enable_thinking 必须为布尔值")
    if "max_output_tokens" in result and (type(result["max_output_tokens"]) is not int or not 32 <= result["max_output_tokens"] <= 16384):
        raise CredentialError("max_output_tokens 必须介于 32 和 16384")
    return result


def normalize_base_url(provider: str, base_url: str | None) -> str:
    provider = normalize_provider(provider)
    if base_url is not None and not isinstance(base_url, str):
        raise CredentialError("API 地址格式无效")
    value = (base_url or PROVIDER_URLS.get(provider) or "").strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise CredentialError("API 地址格式无效") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or port not in (None, 443)):
        raise CredentialError("API 地址须使用公共 HTTPS 地址，且不得包含用户信息、查询参数或自定义端口")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise CredentialError("API 地址不能访问本地或内网")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise CredentialError("API 地址不能访问本地或内网")
    if ".." in parsed.path or any(ord(c) < 32 for c in value):
        raise CredentialError("API 地址格式无效")
    path = parsed.path.rstrip("/")
    if provider in {"openai", "anthropic", "openai-compatible"} and not path:
        path = "/v1"
    if provider == "gemini" and not path:
        path = "/v1beta"
    return urlunsplit(("https", parsed.netloc.lower(), path, "", ""))


async def check_public_endpoint(base_url: str) -> str | None:
    """Return a validated IP for custom hosts, preventing DNS rebinding."""
    host = urlsplit(base_url).hostname
    if host in TRUSTED_HOSTS:
        return
    try:
        answers = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        raise CredentialError("API 地址无法解析") from None
    if not answers or any(not ipaddress.ip_address(answer[4][0]).is_global for answer in answers):
        raise CredentialError("API 地址不能访问本地或内网")
    addresses = [answer[4][0] for answer in answers]
    return next((address for address in addresses if ":" not in address), addresses[0])


def pin_request(url: str, headers: dict, address: str | None):
    if address is None:
        return url, headers, {}
    original = httpx.URL(url)
    pinned = original.copy_with(host=address)
    # Keep TLS verification/SNI and HTTP virtual hosting bound to the requested
    # domain while TCP connects only to the already validated public address.
    return pinned, {**headers, "Host": original.netloc.decode(), "Connection": "close"}, {"sni_hostname": original.host}


@dataclass
class Credential:
    id: str
    owner_id: str = field(repr=False)
    provider: str
    base_url: str
    secret: str = field(repr=False)
    masked_label: str
    created_at: str
    last_verified_at: str | None = None
    temporary: bool = False
    scope_id: str | None = field(default=None, repr=False)
    models: list[dict[str, Any]] = field(default_factory=list)
    revision: int = 1

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "provider": self.provider, "base_url": self.base_url,
                "masked_label": self.masked_label, "created_at": self.created_at,
                "last_verified_at": self.last_verified_at, "temporary": self.temporary,
                "status": "connected" if self.last_verified_at else "unverified",
                "model_count": len(self.models), "models": list(self.models),
                "manual_entry_allowed": True, "revision": self.revision}


def secret_mask(secret: str) -> str:
    # Short secrets must never be reproduced completely by a suffix mask.
    return "••••••" + (secret[-4:] if len(secret) >= 8 else "")


def model_headers(credential: Credential) -> dict[str, str]:
    if credential.provider == "anthropic":
        return {"x-api-key": credential.secret, "anthropic-version": "2023-06-01"}
    if credential.provider == "gemini":
        return {"x-goog-api-key": credential.secret}
    return {"Authorization": f"Bearer {credential.secret}"}


async def discover_models(credential: Credential, client: httpx.AsyncClient) -> list[dict[str, Any]]:
    address = await check_public_endpoint(credential.base_url)
    base = credential.base_url
    if credential.provider == "dashscope":
        base = base if base.endswith("/v1") else base + "/compatible-mode/v1"
    models: dict[str, dict[str, Any]] = {}
    cursor = None
    # Bound pagination prevents an untrusted provider from producing an endless list.
    for _ in range(20):
        params = {}
        if cursor:
            params["pageToken" if credential.provider == "gemini" else "after_id"] = cursor
        url, headers, extensions = pin_request(base + "/models", model_headers(credential), address)
        response = await client.get(url, headers=headers, params=params, follow_redirects=False, extensions=extensions)
        response.raise_for_status()
        body = response.json()
        items = body.get("models", []) if credential.provider == "gemini" else body.get("data", body.get("models", []))
        if not isinstance(items, list):
            raise CredentialError("供应商模型列表格式无效")
        for item in items[:2000]:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id") or item.get("name")
            if credential.provider == "gemini":
                methods = item.get("supportedGenerationMethods", [])
                if methods and "generateContent" not in methods:
                    continue
                raw_id = str(raw_id or "").removeprefix("models/")
            try:
                model_id = validate_model_id(raw_id)
            except CredentialError:
                continue
            if credential.secret in model_id:
                continue
            models[model_id] = {"id": model_id, "model_id": model_id,
                                "provider": credential.provider,
                                "display_name": str(item.get("displayName") or item.get("display_name") or model_id).replace(credential.secret, "••••••")[:200],
                                "source": "discovered"}
        cursor = body.get("nextPageToken") if credential.provider == "gemini" else body.get("last_id") if body.get("has_more") else None
        if not cursor or len(models) >= 2000:
            break
    return sorted(models.values(), key=lambda model: model["id"])


class CredentialService:
    def __init__(self, db_path: str | Path | None = None, encryption_key: str | bytes | None = None):
        if db_path is None:
            game_path = Path(os.getenv("DATABASE_PATH", str(Path(__file__).resolve().parent.parent / "data" / "werewolf.sqlite3")))
            db_path = game_path.with_name("credentials.sqlite3")
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._temporary: dict[str, Credential] = {}
        key = encryption_key or os.getenv("CREDENTIAL_ENCRYPTION_KEY")
        if not key:
            if self.db_path == ":memory:":
                key = Fernet.generate_key()
            else:
                key_path = Path(self.db_path).with_suffix(".key")
                key_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    descriptor = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    key = key_path.read_bytes().strip()
                else:
                    key = Fernet.generate_key()
                    with os.fdopen(descriptor, "wb") as output:
                        output.write(key)
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError):
            raise CredentialError("凭据加密配置无效") from None
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("CREATE TABLE IF NOT EXISTS credentials (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, provider TEXT NOT NULL, base_url TEXT NOT NULL, encrypted_secret TEXT NOT NULL, masked_label TEXT NOT NULL, created_at TEXT NOT NULL, last_verified_at TEXT, models_json TEXT NOT NULL DEFAULT '[]')")
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(credentials)")}
        if "revision" not in columns:
            self._db.execute("ALTER TABLE credentials ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
        self._db.commit()
        if self.db_path != ":memory:":
            os.chmod(self.db_path, 0o600)

    def close(self):
        with self._lock:
            self._temporary.clear()
            self._db.close()

    @staticmethod
    def _owner(owner_id: str):
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise CredentialError("需要用户身份", 401)

    def create(self, owner_id: str, provider: str, secret: str, *, base_url: str | None = None,
               temporary: bool = False, scope_id: str | None = None) -> dict[str, Any]:
        self._owner(owner_id)
        provider = normalize_provider(provider)
        base_url = normalize_base_url(provider, base_url)
        if not isinstance(secret, str) or not secret.strip() or len(secret) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in secret.strip()):
            raise CredentialError("API Key 格式无效")
        if temporary and not scope_id:
            raise CredentialError("临时凭据必须绑定当前会话或房间")
        credential = Credential(secrets.token_urlsafe(18), owner_id, provider, base_url, secret.strip(),
                                secret_mask(secret.strip()), now(), temporary=temporary, scope_id=scope_id)
        if credential.secret in base_url:
            raise CredentialError("API 地址不得包含 API Key")
        with self._lock:
            if temporary:
                self._temporary[credential.id] = credential
            else:
                encrypted = self._fernet.encrypt(credential.secret.encode()).decode()
                self._db.execute("INSERT INTO credentials(id,owner_id,provider,base_url,encrypted_secret,masked_label,created_at) VALUES(?,?,?,?,?,?,?)",
                                 (credential.id, owner_id, provider, base_url, encrypted, credential.masked_label, credential.created_at))
                self._db.commit()
        return credential.public()

    def resolve(self, owner_id: str, credential_id: str, *, scope_id: str | None = None) -> Credential:
        self._owner(owner_id)
        with self._lock:
            temporary = self._temporary.get(credential_id)
            if temporary:
                if temporary.owner_id != owner_id or temporary.scope_id not in (scope_id, "session:" + owner_id):
                    raise CredentialError("凭据不存在或无权访问", 404)
                return replace(temporary, models=deepcopy(temporary.models))
            row = self._db.execute("SELECT * FROM credentials WHERE id=? AND owner_id=?", (credential_id, owner_id)).fetchone()
        if not row:
            raise CredentialError("凭据不存在或无权访问", 404)
        try:
            secret = self._fernet.decrypt(row["encrypted_secret"].encode()).decode()
        except (InvalidToken, UnicodeError):
            raise CredentialError("凭据无法解密，请重新添加密钥", 409) from None
        return Credential(row["id"], owner_id, row["provider"], row["base_url"], secret,
                          row["masked_label"], row["created_at"], row["last_verified_at"], models=json.loads(row["models_json"]), revision=row["revision"])

    def get(self, owner_id: str, credential_id: str, *, scope_id: str | None = None) -> dict[str, Any]:
        return self.resolve(owner_id, credential_id, scope_id=scope_id).public()

    def list(self, owner_id: str, *, scope_id: str | None = None) -> list[dict[str, Any]]:
        self._owner(owner_id)
        with self._lock:
            rows = self._db.execute("SELECT id,provider,base_url,masked_label,created_at,last_verified_at,models_json,revision FROM credentials WHERE owner_id=? ORDER BY created_at", (owner_id,)).fetchall()
            result = [{"id": row["id"], "provider": row["provider"], "base_url": row["base_url"],
                       "masked_label": row["masked_label"], "created_at": row["created_at"],
                       "last_verified_at": row["last_verified_at"], "temporary": False,
                       "status": "connected" if row["last_verified_at"] else "unverified",
                       "models": json.loads(row["models_json"]), "model_count": len(json.loads(row["models_json"])),
                       "manual_entry_allowed": True, "revision": row["revision"]} for row in rows]
            result.extend(credential.public() for credential in self._temporary.values()
                          if credential.owner_id == owner_id and credential.scope_id in (scope_id, "session:" + owner_id))
        return result

    def replace(self, owner_id: str, credential_id: str, secret: str, *, base_url: str | None = None,
                scope_id: str | None = None) -> dict[str, Any]:
        credential = self.resolve(owner_id, credential_id, scope_id=scope_id)
        if not isinstance(secret, str) or not secret.strip() or len(secret) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in secret.strip()):
            raise CredentialError("API Key 格式无效")
        base_url = normalize_base_url(credential.provider, base_url or credential.base_url)
        if secret.strip() in base_url:
            raise CredentialError("API 地址不得包含 API Key")
        with self._lock:
            credential.secret, credential.masked_label, credential.base_url = secret.strip(), secret_mask(secret.strip()), base_url
            credential.last_verified_at, credential.models = None, []
            credential.revision += 1
            if credential.temporary:
                self._temporary[credential.id] = credential
            else:
                self._db.execute("UPDATE credentials SET encrypted_secret=?,masked_label=?,base_url=?,last_verified_at=NULL,models_json='[]',revision=revision+1 WHERE id=? AND owner_id=?",
                                 (self._fernet.encrypt(credential.secret.encode()).decode(), credential.masked_label, base_url, credential.id, owner_id))
                self._db.commit()
        return credential.public()

    def delete(self, owner_id: str, credential_id: str, *, scope_id: str | None = None) -> None:
        credential = self.resolve(owner_id, credential_id, scope_id=scope_id)
        with self._lock:
            if credential.temporary:
                self._temporary.pop(credential_id, None)
            else:
                self._db.execute("DELETE FROM credentials WHERE id=? AND owner_id=?", (credential_id, owner_id))
                self._db.commit()

    def clear_scope(self, scope_id: str) -> None:
        with self._lock:
            self._temporary = {key: credential for key, credential in self._temporary.items() if credential.scope_id != scope_id}

    def validate_route(self, owner_id: str, credential_id: str, model_id: str, *, scope_id: str | None = None) -> dict[str, Any]:
        credential = self.resolve(owner_id, credential_id, scope_id=scope_id)
        model_id = validate_model_id(model_id).removeprefix("models/") if credential.provider == "gemini" else validate_model_id(model_id)
        if credential.secret in model_id:
            raise CredentialError("模型 ID 不得包含 API Key")
        return {"credential_id": credential.id, "credential_owner_id": owner_id,
                "credential_scope_id": scope_id, "provider": credential.provider, "model_id": model_id,
                "model_key": f"{credential.provider}:{model_id}", "credential_revision": credential.revision}

    def route_valid(self, owner_id: str, credential_id: str, revision: int, *, scope_id: str | None = None) -> bool:
        try:
            return self.resolve(owner_id, credential_id, scope_id=scope_id).revision == revision
        except CredentialError:
            return False

    def _verified(self, credential: Credential, models: list[dict[str, Any]]):
        with self._lock:
            if not self.route_valid(credential.owner_id, credential.id, credential.revision, scope_id=credential.scope_id):
                raise CredentialError("凭据已更新或删除，请重新测试", 409)
            credential.last_verified_at, credential.models = now(), models
            if credential.temporary:
                self._temporary[credential.id] = credential
            else:
                self._db.execute("UPDATE credentials SET last_verified_at=?,models_json=? WHERE id=? AND owner_id=?",
                                 (credential.last_verified_at, json.dumps(models, ensure_ascii=False), credential.id, credential.owner_id))
                self._db.commit()

    async def discover(self, owner_id: str, credential_id: str, *, scope_id: str | None = None,
                       client: httpx.AsyncClient | None = None) -> dict[str, Any]:
        credential = self.resolve(owner_id, credential_id, scope_id=scope_id)
        own_client = client is None
        client = client or httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False)
        try:
            models = await discover_models(credential, client)
            self._verified(credential, models)
            return {"credential": credential.public(), "models": models, "verified": True, "manual_entry_allowed": True}
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            error = "供应商拒绝了密钥，请检查密钥和账号权限" if status in (401, 403) else "供应商未提供可用的模型列表，可手动输入模型 ID"
            return {"credential": credential.public(), "models": list(credential.models), "verified": False,
                    "manual_entry_allowed": True, "error": error, "error_code": f"provider_http_{status}"}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return {"credential": credential.public(), "models": list(credential.models), "verified": False,
                    "manual_entry_allowed": True, "error": "无法获取模型列表，请检查 API 地址和网络后重试", "error_code": "model_discovery_failed"}
        finally:
            if own_client:
                await client.aclose()

    async def test(self, owner_id: str, credential_id: str, *, scope_id: str | None = None,
                   model_id: str | None = None, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
        result = await self.discover(owner_id, credential_id, scope_id=scope_id, client=client)
        if result["verified"] or not model_id or result.get("error_code") in {"provider_http_401", "provider_http_403"}:
            return result
        credential = self.resolve(owner_id, credential_id, scope_id=scope_id)
        model_id = validate_model_id(model_id)
        own_client = client is None
        client = client or httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False)
        try:
            from .providers import connection_probe
            await connection_probe(credential, model_id, client)
            self._verified(credential, credential.models)
            return {"credential": credential.public(), "models": credential.models, "verified": True,
                    "manual_entry_allowed": True, "probe_model_id": model_id}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return {**result, "error": "连接测试失败，请检查模型 ID、密钥和账号权限", "error_code": "connection_test_failed"}
        finally:
            if own_client:
                await client.aclose()

    refresh = discover

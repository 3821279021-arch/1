"""Run inside the built image; validates real ASGI startup and session storage."""

import json
import os
import subprocess
import tempfile
import time

import httpx

with tempfile.TemporaryDirectory() as folder:
    env = {
        **os.environ,
        "DATABASE_PATH": folder + "/container.sqlite",
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "GEMINI_API_KEY": "",
        "DASHSCOPE_API_KEY": "",
    }
    server = subprocess.Popen(
        ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8010"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(base_url="http://127.0.0.1:8010", trust_env=False, timeout=2) as client:
            for _ in range(60):
                try:
                    health = client.get("/api/health")
                    if health.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Container ASGI startup failed")
            assert health.json()["version"] == "3.2.0"
            session = client.post("/api/session").json()
            headers = {"Authorization": "Bearer " + session["token"]}
            room = client.post("/api/rooms", headers=headers, json={}).json()
            assert room["phase"] == "lobby" and room["is_host"]
            recovered = client.post("/api/session/recover", json={"code": session["recovery_code"]}).json()
            assert recovered["owner_id"] == session["owner_id"]
            assert client.get("/api/rooms", headers=headers).status_code == 401
            print(
                json.dumps(
                    {
                        "version": "3.2.0",
                        "asgi_health": True,
                        "sqlite_room_creation": True,
                        "session_recovery_and_revocation": True,
                    }
                )
            )
    finally:
        server.terminate()
        server.wait(timeout=10)

"""V3 HTTP acceptance: real multiplayer commands, complete Mock games and review.

These tests exercise the application/storage/orchestration boundaries. Provider
transport and individual role mechanics are covered by the other V3 suites.
"""

import asyncio
import base64
import json
import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


def acceptance_environment(folder, **extra):
    return patch.dict(
        os.environ,
        {
            "DATABASE_PATH": folder + "/acceptance.sqlite",
            "GAME_TIME_SCALE": "1",
            "AI_TURN_PAUSE": "0",
            "AI_CHUNK_DELAY": "0",
            "OPENAI_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
            "GEMINI_API_KEY": "",
            "DASHSCOPE_API_KEY": "",
            "RATE_SESSION_PER_MINUTE": "0",
            "RATE_ROOM_PER_HOUR": "0",
            "RATE_ACTION_PER_MINUTE": "0",
            "RATE_JOIN_PER_MINUTE": "0",
            "RATE_RECONNECT_PER_MINUTE": "0",
            "RATE_ROOM_AI_PER_MINUTE": "0",
            "CREDENTIAL_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"V3" * 16).decode(),
            **extra,
        },
        clear=True,
    )


@contextmanager
def application(**extra):
    with tempfile.TemporaryDirectory() as folder, acceptance_environment(folder, **extra), TestClient(app) as client:
        yield client, folder


class V3APIIntegrationTests(unittest.TestCase):
    def session(self, client):
        result = client.post("/api/session")
        self.assertEqual(result.status_code, 200, result.text)
        session = result.json()
        return session, {"Authorization": "Bearer " + session["token"]}

    def post(self, client, path, headers, body=None):
        result = client.post(path, headers=headers, json=body or {})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def stop_clock(self, client):
        async def stop():
            app.state.manager.runner.cancel()
            await asyncio.gather(app.state.manager.runner, return_exceptions=True)
            app.state.manager.runner = None

        client.portal.call(stop)

    def prepare(self, client, room_id, callback):
        async def mutate():
            room = await app.state.manager.get_async(room_id)
            async with room.lock:
                callback(room)
                await app.state.manager.commit_async(room)

        client.portal.call(mutate)

    def action(self, client, room_id, headers, **values):
        state = client.get(f"/api/rooms/{room_id}", headers=headers).json()
        pending = state["pending_action"]
        self.assertIsNotNone(pending)
        return self.post(
            client,
            f"/api/rooms/{room_id}/action",
            headers,
            {
                "game_id": state["game_id"],
                "turn_id": state["turn_id"],
                "turn_sequence": state["turn_sequence"],
                "action": pending["type"],
                **values,
            },
        )

    def assert_review_blocked(self, response):
        if response.status_code == 200:
            self.assertFalse(response.json().get("details_available", True), response.text)
            self.assertFalse(response.json().get("players"), response.text)
            self.assertFalse(response.json().get("events"), response.text)
        else:
            self.assertIn(response.status_code, (400, 403, 404), response.text)

    def test_random_join_all_nine_seats_optional_change_and_owner_boundary(self):
        with application() as (client, _):
            self.stop_clock(client)
            _, host = self.session(client)
            state = self.post(client, "/api/rooms", host, {"name": "房主", "mode": "standard9"})
            room_id = state["room_id"]
            self.assertEqual(state["player_count"], 9)
            self.assertIn(state["self"]["id"], range(1, 10))
            for index in range(8):
                _, member = self.session(client)
                state = self.post(client, f"/api/rooms/{room_id}/join", member, {"name": f"成员{index}"})
                same = self.post(client, f"/api/rooms/{room_id}/join", member, {"name": "再次加入"})
                self.assertEqual(state["self"]["id"], same["self"]["id"])
            self.assertEqual(sorted(p["id"] for p in state["players"]), list(range(1, 10)))
            _, stranger = self.session(client)
            self.assertEqual(client.get(f"/api/rooms/{room_id}", headers=stranger).status_code, 403)
            self.assertEqual(client.post(f"/api/rooms/{room_id}/join", headers=stranger, json={}).status_code, 400)
            self.assertEqual(
                client.post(f"/api/rooms/{room_id}/seat", headers=stranger, json={"seat": 1}).status_code, 403
            )
            current = client.get(f"/api/rooms/{room_id}", headers=host).json()["self"]["id"]
            occupied = next(p["id"] for p in state["players"] if p["id"] != current)
            self.assertEqual(
                client.post(f"/api/rooms/{room_id}/seat", headers=host, json={"seat": occupied}).status_code, 400
            )
            spare = self.post(client, "/api/rooms", host, {"mode": "standard12", "seat": 1})
            random_change = self.post(client, f"/api/rooms/{spare['room_id']}/seat", host, {"seat": None})
            self.assertIn(random_change["self"]["id"], range(1, 13))
            moved = self.post(client, f"/api/rooms/{spare['room_id']}/seat", host, {"seat": 12})
            self.assertEqual(moved["self"]["id"], 12)
            configured = self.post(
                client,
                f"/api/rooms/{spare['room_id']}/configure",
                host,
                {"pace": "fast", "seats": [{"id": 11, "provider": "mock"}]},
            )
            self.assertIn("11", configured["seat_presets"])
            self.assertEqual(
                client.post(
                    f"/api/rooms/{spare['room_id']}/configure",
                    headers=host,
                    json={"seats": [{"id": 13, "provider": "mock"}]},
                ).status_code,
                400,
            )

    def test_http_completion_advances_before_deadline_and_preserves_pending_actors(self):
        with application() as (client, _):
            self.stop_clock(client)
            members = []
            for _ in range(9):
                members.append(self.session(client)[1])
            state = self.post(client, "/api/rooms", members[0], {"mode": "standard9", "seat": 1})
            room_id = state["room_id"]
            for seat in range(2, 10):
                self.post(client, f"/api/rooms/{room_id}/join", members[seat - 1], {"seat": seat})
            self.post(client, f"/api/rooms/{room_id}/start", members[0])
            roles = ["wolf", "wolf", "wolf", "seer", "witch", "hunter", "villager", "villager", "villager"]

            def stage(room):
                for player, role in zip(room.game.players, roles):
                    player.role = role
                room.engine.enter("night_discussion")

            self.prepare(client, room_id, stage)
            for index in range(3):
                previous = client.get(f"/api/rooms/{room_id}", headers=members[index]).json()
                self.assertGreater(previous["turn_deadline"], time.time())
                state = self.action(client, room_id, members[index], text="讨论完成，请进入选择阶段。")
                self.assertEqual(state["phase"], "night_discussion" if index < 2 else "night_wolves")
            for index in range(3):
                state = self.action(client, room_id, members[index], target=9)
                self.assertEqual(state["phase"], "night_wolves" if index < 2 else "night_seer")
            state = self.action(client, room_id, members[3], target=1)
            self.assertEqual(state["phase"], "night_witch")
            state = self.action(client, room_id, members[4], save=False, poison_target=None)
            self.assertNotEqual(state["phase"], "night_witch")

            def speech(room):
                room.game.speech_queue = [5]
                room.engine.enter("day_speech", 4)

            self.prepare(client, room_id, speech)
            before = client.get(f"/api/rooms/{room_id}", headers=members[3]).json()
            state = self.action(client, room_id, members[3], speech="我结束发言，请下一位。")
            self.assertEqual(state["current_turn_player_id"], 5)
            self.assertGreater(before["turn_deadline"], time.time())

            def vote(room):
                for player in room.game.players:
                    player.alive = True
                room.engine.enter("day_vote")

            self.prepare(client, room_id, vote)
            before = client.get(f"/api/rooms/{room_id}", headers=members[0]).json()
            for index in range(9):
                state = self.action(client, room_id, members[index], target=None)
                self.assertEqual(state["phase"], "day_vote" if index < 8 else "night_discussion")
            self.assertGreater(before["turn_deadline"], time.time())

    def test_custom_mode_invalid_board_and_private_pre_finish_review(self):
        with application() as (client, _):
            self.stop_clock(client)
            _, host = self.session(client)
            for body in [
                {"mode": "custom", "player_count": 8, "roles": ["wolf", "villager"]},
                {"mode": "custom", "player_count": 4, "roles": ["wolf", "wolf", "villager", "villager"]},
                {"mode": "custom", "roles": ["fake-role", "villager", "villager", "villager"]},
            ]:
                self.assertIn(client.post("/api/rooms", headers=host, json=body).status_code, (400, 422))
            roles = ["wolf", "seer", "guard", "hunter", "villager", "villager", "idiot", "knight"]
            state = self.post(client, "/api/rooms", host, {"mode": "custom", "player_count": 8, "roles": roles})
            self.assertEqual(state["player_count"], 8)
            room_id = state["room_id"]
            self.post(client, f"/api/rooms/{room_id}/start", host)
            state = client.get(f"/api/rooms/{room_id}", headers=host).json()
            self.assertEqual(len(state["players"]), 8)
            self.assertFalse(any(p["role"] for p in state["players"] if not p["is_you"]))
            for suffix in ("analysis", "replay"):
                self.assert_review_blocked(client.get(f"/api/rooms/{room_id}/{suffix}", headers=host))
            _, stranger = self.session(client)
            for suffix in ("games", "analysis", "replay"):
                self.assertEqual(client.get(f"/api/rooms/{room_id}/{suffix}", headers=stranger).status_code, 403)

    def test_byok_owner_isolation_masking_binding_and_secret_storage(self):
        with application() as (client, folder):
            self.stop_clock(client)
            _, host = self.session(client)
            _, other = self.session(client)
            secret = "sk-v3-owner-secret-sentinel-ABCD"
            validation = client.post(
                "/api/credentials", headers=host, json={"provider": "not-a-provider", "api_key": secret}
            )
            self.assertIn(validation.status_code, (400, 422), validation.text)
            self.assertNotIn(secret, validation.text)
            body = {"provider": "openai-compatible", "base_url": "https://api.openai.com/v1", "api_key": secret}
            created = self.post(client, "/api/credentials", host, body)
            credential = created.get("credential", created)
            credential_id = credential["id"]
            self.assertIn("masked_label", credential)
            self.assertNotIn(secret, json.dumps(created))
            self.assertNotIn("encrypted_secret", json.dumps(created))
            listed = client.get("/api/credentials", headers=host)
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertIn(credential_id, json.dumps(listed.json()))
            outsider_list = client.get("/api/credentials", headers=other)
            self.assertEqual(outsider_list.status_code, 200, outsider_list.text)
            self.assertNotIn(credential_id, json.dumps(outsider_list.json()))
            for method, suffix, payload in [
                ("get", "", None),
                ("put", "", {"api_key": "sk-other-user-key"}),
                ("delete", "", None),
                ("post", "/test", {}),
                ("post", "/models", {}),
            ]:
                kwargs = {"headers": other}
                if payload is not None:
                    kwargs["json"] = payload
                response = getattr(client, method)(f"/api/credentials/{credential_id}{suffix}", **kwargs)
                self.assertIn(response.status_code, (403, 404), response.text)

            state = self.post(client, "/api/rooms", host, {"mode": "standard9", "seat": 1})
            room_id = state["room_id"]
            self.post(client, f"/api/rooms/{room_id}/join", other, {"seat": 2})
            other_created = self.post(
                client, "/api/credentials", other, {"provider": "openai", "api_key": "sk-second-owner-sentinel-WXYZ"}
            )
            other_id = other_created.get("credential", other_created)["id"]
            forbidden = client.post(
                f"/api/rooms/{room_id}/configure",
                headers=host,
                json={"seats": [{"id": 3, "credential_id": other_id, "model_id": "manual-one"}]},
            )
            self.assertIn(forbidden.status_code, (400, 403, 404), forbidden.text)
            self.post(
                client,
                f"/api/rooms/{room_id}/configure",
                host,
                {
                    "seats": [
                        {"id": 3, "credential_id": credential_id, "model_id": "manual-one"},
                        {"id": 4, "credential_id": credential_id, "model_id": "manual-two"},
                    ]
                },
            )
            responses = [
                client.get("/api/health").json(),
                client.get(f"/api/rooms/{room_id}", headers=host).json(),
                client.get(f"/api/rooms/{room_id}", headers=other).json(),
            ]
            with client.websocket_connect(f"/ws/{room_id}") as websocket:
                token = host["Authorization"].removeprefix("Bearer ")
                websocket.send_json({"token": token})
                responses.append(websocket.receive_json())
            for response in responses:
                self.assertNotIn(secret, json.dumps(response))
                self.assertNotIn("encrypted_secret", json.dumps(response))
            for path in Path(folder).glob("acceptance.sqlite*"):
                self.assertNotIn(secret.encode(), path.read_bytes())

            replacement = "sk-v3-replacement-secret-sentinel-EFGH"
            changed = client.put(f"/api/credentials/{credential_id}", headers=host, json={"api_key": replacement})
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertNotIn(replacement, changed.text)
            changed = changed.json().get("credential", changed.json())
            self.assertGreater(changed["revision"], credential["revision"])
            deleted = client.delete(f"/api/credentials/{credential_id}", headers=host)
            self.assertIn(deleted.status_code, (200, 204), deleted.text)
            self.assertIn(client.get(f"/api/credentials/{credential_id}", headers=host).status_code, (400, 404))
            self.assertNotIn(credential_id, client.get("/api/credentials", headers=host).text)
            revoked = client.post(
                f"/api/rooms/{room_id}/configure",
                headers=host,
                json={"seats": [{"id": 5, "credential_id": credential_id, "model_id": "manual-one"}]},
            )
            self.assertIn(revoked.status_code, (400, 403, 404), revoked.text)
            start = client.post(f"/api/rooms/{room_id}/start", headers=host, json={})
            self.assertEqual(start.status_code, 400, start.text)
            self.assertEqual(client.get(f"/api/rooms/{room_id}", headers=host).json()["phase"], "lobby")

    def test_credential_persistence_and_temporary_room_scope_after_restart(self):
        with tempfile.TemporaryDirectory() as folder, acceptance_environment(folder):
            with TestClient(app) as client:
                self.stop_clock(client)
                _, host = self.session(client)
                room = self.post(client, "/api/rooms", host, {})
                room_id = room["room_id"]
                permanent = self.post(
                    client, "/api/credentials", host, {"provider": "openai", "api_key": "sk-v3-durable-sentinel-1234"}
                )
                permanent_id = permanent.get("credential", permanent)["id"]
                temporary = self.post(
                    client,
                    "/api/credentials",
                    host,
                    {
                        "provider": "openai",
                        "api_key": "sk-v3-temporary-sentinel-5678",
                        "temporary": True,
                        "scope_id": room_id,
                    },
                )
                temporary_id = temporary.get("credential", temporary)["id"]
                scoped = client.get("/api/credentials", headers=host, params={"scope_id": room_id})
                self.assertEqual(scoped.status_code, 200, scoped.text)
                self.assertIn(temporary_id, scoped.text)
                other_room = self.post(client, "/api/rooms", host, {})
                wrong_scope = client.post(
                    f"/api/rooms/{other_room['room_id']}/configure",
                    headers=host,
                    json={
                        "seats": [
                            {
                                "id": 2 if other_room["self"]["id"] != 2 else 3,
                                "credential_id": temporary_id,
                                "model_id": "manual-one",
                            }
                        ]
                    },
                )
                self.assertIn(wrong_scope.status_code, (400, 403, 404), wrong_scope.text)
            with TestClient(app) as client:
                self.stop_clock(client)
                restored = client.get(f"/api/credentials/{permanent_id}", headers=host)
                self.assertEqual(restored.status_code, 200, restored.text)
                self.assertNotIn("sk-v3-durable-sentinel-1234", restored.text)
                lost = client.get(f"/api/credentials/{temporary_id}", headers=host, params={"scope_id": room_id})
                self.assertIn(lost.status_code, (400, 404), lost.text)

    def test_deleted_bound_credential_cancels_old_result_and_never_borrows_platform_key(self):
        with application(OPENAI_API_KEY="sk-v3-platform-key-must-not-be-used") as (client, _):
            self.stop_clock(client)
            _, host = self.session(client)
            credential = self.post(
                client, "/api/credentials", host, {"provider": "openai", "api_key": "sk-v3-bound-user-key-sentinel"}
            )
            credential_id = credential.get("credential", credential)["id"]
            room_id = self.post(client, "/api/rooms", host, {"seat": 1})["room_id"]
            self.post(
                client,
                f"/api/rooms/{room_id}/configure",
                host,
                {"seats": [{"id": 3, "credential_id": credential_id, "model_id": "manual-one"}]},
            )
            self.post(client, f"/api/rooms/{room_id}/start", host)
            self.prepare(client, room_id, lambda room: room.engine.enter("day_vote"))
            manager = app.state.manager
            original = manager.ai.propose
            begun, release = asyncio.Event(), asyncio.Event()

            async def blocked(view, *args, **kwargs):
                begun.set()
                await release.wait()
                return {
                    "game_id": view["game_id"],
                    "turn_id": view["turn_id"],
                    "turn_sequence": view["turn_sequence"],
                    "action": "vote",
                    "target": 1,
                }

            async def begin_job():
                room = manager.rooms[room_id]
                task = asyncio.create_task(manager.actor(room, 3, room.game.turn_sequence))
                room.jobs[(room.game.turn_sequence, 3)] = task
                await asyncio.wait_for(begun.wait(), 2)

            manager.ai.propose = blocked
            try:
                client.portal.call(begin_job)
                result = client.delete(f"/api/credentials/{credential_id}", headers=host)
                self.assertEqual(result.status_code, 200, result.text)

                async def settle():
                    release.set()
                    await asyncio.sleep(0.01)

                client.portal.call(settle)
                self.assertNotIn("3", manager.rooms[room_id].game.votes)
            finally:
                manager.ai.propose = original

            requests = []

            async def forbid_transport(*args, **kwargs):
                requests.append(True)
                raise AssertionError("A revoked BYOK seat must not use a platform key")

            with patch("httpx.AsyncClient.request", new=forbid_transport):
                room = manager.rooms[room_id]
                client.portal.call(manager.actor, room, 3, room.game.turn_sequence)
            self.assertEqual(requests, [])
            self.assertIn("3", room.game.votes)
            self.assertIsNone(room.game.votes["3"])
            self.assertEqual(room.game.player(3).execution_status.get("failure_reason"), "credential_unavailable")

    def test_finished_history_survives_restart_and_new_member_cannot_read_old_game(self):
        with tempfile.TemporaryDirectory() as folder, acceptance_environment(folder):
            with TestClient(app) as client:
                self.stop_clock(client)
                _, host = self.session(client)
                _, newcomer = self.session(client)
                state = self.post(client, "/api/rooms", host, {"seat": 1})
                room_id = state["room_id"]
                state = self.post(client, f"/api/rooms/{room_id}/start", host)
                game_id = state["game_id"]
                private_note = "V3-private-note-must-never-appear-in-public-replay"

                def finish_fixture(room):
                    room.engine.emit("private_note", {"note": private_note}, "player", 1)
                    room.engine.emit("wolf_chat_message", {"speech": "V3-private-wolf-chat-sentinel"}, "wolves")
                    room.engine.finish("draw")

                self.prepare(client, room_id, finish_fixture)
                old = client.get(f"/api/rooms/{room_id}/replay", headers=host).json()
                stats = client.get(f"/api/rooms/{room_id}/analysis", headers=host).json()
                self.assertEqual(stats["winner"], "draw")
                self.assertTrue(all(not player["won"] for player in stats["players"]))
                self.assertNotIn(private_note, json.dumps([old, stats]))
                self.assertNotIn("V3-private-wolf-chat-sentinel", json.dumps([old, stats]))
                self.post(client, f"/api/rooms/{room_id}/rematch", host)
                self.post(client, f"/api/rooms/{room_id}/join", newcomer, {})
                for suffix in ("analysis", "replay"):
                    blocked = client.get(
                        f"/api/rooms/{room_id}/{suffix}", headers=newcomer, params={"game_id": game_id}
                    )
                    self.assertEqual(blocked.status_code, 403, blocked.text)
            with TestClient(app) as client:
                self.stop_clock(client)
                response = client.get(f"/api/rooms/{room_id}/replay", headers=host, params={"game_id": game_id})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), old)
                report = client.get(f"/api/rooms/{room_id}/analysis", headers=host, params={"game_id": game_id})
                self.assertEqual(report.status_code, 200, report.text)
                self.assertEqual(report.json(), stats)

    def test_all_modes_complete_mock_game_and_rematch_preserves_replay(self):
        results = []
        cases = [
            ("quick6", {}, 6),
            ("standard9", {}, 9),
            ("standard12", {}, 12),
            (
                "custom",
                {
                    "player_count": 8,
                    "roles": ["wolf", "wolf_king", "seer", "witch", "guard", "hunter", "knight", "idiot"],
                },
                8,
            ),
        ]
        with application(GAME_TIME_SCALE="0.05") as (client, _):
            _, host = self.session(client)
            for mode, options, count in cases:
                with self.subTest(mode=mode):
                    state = self.post(client, "/api/rooms", host, {"mode": mode, "pace": "fast", **options})
                    room_id = state["room_id"]
                    configured = client.patch(
                        f"/api/rooms/{room_id}/pet", headers=host, json={"control_mode": "autopilot"}
                    )
                    self.assertEqual(configured.status_code, 200, configured.text)
                    started = time.monotonic()
                    state = self.post(client, f"/api/rooms/{room_id}/start", host)
                    game_id = state["game_id"]
                    while not state["game_over"] and time.monotonic() - started < 25:
                        time.sleep(0.015)
                        response = client.get(f"/api/rooms/{room_id}", headers=host)
                        self.assertEqual(response.status_code, 200, response.text)
                        state = response.json()
                    self.assertTrue(state["game_over"], f"{mode}: phase={state['phase']}, day={state['day']}")
                    self.assertEqual(len(state["players"]), count)
                    self.assertTrue(all(p["role"] for p in state["players"]))
                    self.assertIn(state["winner"], {"wolves", "good", "draw"})
                    replay = client.get(f"/api/rooms/{room_id}/replay", headers=host)
                    analysis = client.get(f"/api/rooms/{room_id}/analysis", headers=host)
                    self.assertEqual(replay.status_code, 200, replay.text)
                    self.assertEqual(analysis.status_code, 200, analysis.text)
                    replay = replay.json()
                    analysis = analysis.json()
                    self.assertEqual(replay["game_id"], game_id)
                    self.assertEqual(analysis["game_id"], game_id)
                    self.assertEqual(analysis["winner"], state["winner"])
                    self.assertEqual(len(analysis["players"]), count)
                    self.assertTrue(replay["events"])
                    self.assertTrue(all(event.get("audience", "public") == "public" for event in replay["events"]))
                    serialized = json.dumps([replay, analysis], ensure_ascii=False)
                    self.assertNotIn("encrypted_secret", serialized)
                    self.assertNotIn("owner_id", serialized)
                    results.append(
                        {
                            "mode": mode,
                            "player_count": count,
                            "winner": state["winner"],
                            "days": state["day"],
                            "elapsed_seconds": round(time.monotonic() - started, 3),
                            "events": len(replay["events"]),
                            "review_players": len(analysis["players"]),
                        }
                    )

                    if mode == "standard9":
                        old_replay = replay
                        rematch = self.post(client, f"/api/rooms/{room_id}/rematch", host)
                        self.assertNotEqual(rematch["game_id"], game_id)
                        self.assert_review_blocked(client.get(f"/api/rooms/{room_id}/analysis", headers=host))
                        old = client.get(f"/api/rooms/{room_id}/replay", headers=host, params={"game_id": game_id})
                        self.assertEqual(old.status_code, 200, old.text)
                        self.assertEqual(old.json(), old_replay)
                        history = client.get(f"/api/rooms/{room_id}/games", headers=host)
                        self.assertEqual(history.status_code, 200, history.text)
                        self.assertIn(game_id, json.dumps(history.json()))
            output = Path(__file__).resolve().parents[1] / "test-artifacts" / "v3-api-results.json"
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()

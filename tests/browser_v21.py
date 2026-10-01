"""Browser regression for event-only rendering and mobile/audio interaction.

Run against any project static server; API and WebSocket responses are isolated
fixtures, so this test never calls a paid model. SpeechSynthesis is instrumented
rather than claiming a headless browser can prove real phone audibility.
"""

import copy
import json
import os
import time
from pathlib import Path

from browser_helpers import wait_condition
from playwright.sync_api import sync_playwright

BASE = os.getenv("TEST_BASE_URL", "http://127.0.0.1:8000")
OUT = Path(__file__).resolve().parents[1] / "test-artifacts"
OUT.mkdir(exist_ok=True)
ROOM = "abcde12345"


def snapshot():
    return {
        "room_id": ROOM,
        "game_id": "browser-fixture-game",
        "state_revision": 10,
        "title": "浏览器验收",
        "pace": "fast",
        "day": 1,
        "phase": "lobby",
        "phase_name": "等待玩家",
        "game_over": False,
        "winner": None,
        "lifecycle": "LOBBY",
        "current_turn_player_id": None,
        "turn_started_at": time.time(),
        "turn_deadline": time.time() + 30,
        "turn_duration": 30,
        "turn_sequence": 0,
        "turn_id": "turn-0",
        "server_time": time.time(),
        "current_speech": "",
        "is_host": True,
        "unique_model_per_ai_seat": True,
        "seat_presets": {
            str(i): {
                "id": i,
                "provider": "mock",
                "model_key": "mock:mock",
                "model_locked": True,
                "personality": "detective",
            }
            for i in range(2, 7)
        },
        "model_registry": [
            {
                "key": "mock:mock",
                "provider": "mock",
                "model": "mock",
                "configured": True,
                "enabled": True,
                "healthy": True,
            },
            {
                "key": "dashscope:qwen-plus",
                "provider": "dashscope",
                "model": "qwen-plus",
                "configured": True,
                "enabled": True,
                "healthy": True,
            },
            {
                "key": "openai:example",
                "provider": "openai",
                "model": "example",
                "configured": False,
                "enabled": True,
                "healthy": True,
            },
        ],
        "provider_status": {"dashscope": {"configured": True}, "mock": {"configured": True}},
        "players": [
            {
                "id": 1,
                "name": "玩家",
                "alive": True,
                "is_human": True,
                "is_you": True,
                "provider": "human",
                "personality": "detective",
                "role": None,
            }
        ],
        "self": {"id": 1, "name": "玩家", "alive": True, "role": None, "role_key": None, "private_notes": []},
        "events": [],
        "vote_status": {},
        "pending_action": None,
        "pet": {
            "name": "月牙",
            "personality": "detective",
            "control_mode": "copilot",
            "provider": "mock",
            "delegate_next": False,
            "play_style": {},
            "private_chat_history": [],
        },
    }


BOOT = r"""
window.__speech=[]; window.__audioCancels=0; window.__wakeRequests=0; window.__wakeReleases=0;
Object.defineProperty(window,'speechSynthesis',{configurable:true,value:{
 getVoices:()=>[{name:'中文一',voiceURI:'voice-1',lang:'zh-CN'},{name:'中文二',voiceURI:'voice-2',lang:'zh-CN'}],
 speak:u=>{window.__speech.push({text:u.text,lang:u.lang,rate:u.rate,pitch:u.pitch,voice:u.voice?.voiceURI});setTimeout(()=>u.onend?.(),5);},
 cancel:()=>window.__audioCancels++,resume:()=>{}
}});
window.SpeechSynthesisUtterance=class{constructor(text){this.text=text;}};
Object.defineProperty(navigator,'wakeLock',{configurable:true,value:{request:async()=>{
 window.__wakeRequests++;return {addEventListener:()=>{},release:async()=>{window.__wakeReleases++;}};
}}});
class FixtureSocket {
 static OPEN=1; constructor(){this.readyState=1;window.__socket=this;setTimeout(()=>this.onopen?.(),10);}
 send(text){if(text!=='ping'){setTimeout(()=>this.onmessage?.({data:JSON.stringify({type:'state_snapshot',data:window.__fixtureState})}),5);}}
 close(){this.readyState=3;}
}
window.WebSocket=FixtureSocket;
window.__packet=p=>window.__socket.onmessage({data:JSON.stringify(p)});
"""

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, service_workers="block"
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    current = snapshot()
    held_pet = []
    action_requests = []
    held_actions = []
    session_creations = []
    flags = {"expire_mutation": False, "configure_calls": 0}
    page.add_init_script(
        "localStorage.setItem('werewolf-v2-token','expired-fixture-token');\n"
        + BOOT
        + "\nwindow.__fixtureState="
        + json.dumps(current, ensure_ascii=False)
        + ";"
    )

    def route_api(route):
        request = route.request
        path = request.url.split("/api", 1)[1]
        if path == "/session":
            session_creations.append(True)
            data = {"token": "browser-fixture-token", "owner_id": "browser-fixture-owner"}
        elif path == "/rooms" and request.method == "GET":
            if request.headers.get("authorization") == "Bearer expired-fixture-token":
                route.fulfill(status=401, content_type="application/json", body=json.dumps({"detail": "expired"}))
                return
            data = []
        elif path == "/pet/memory":
            data = {"preferences": {"advice_length": "short"}}
        elif path.endswith("/configure") and flags["expire_mutation"]:
            flags["configure_calls"] += 1
            route.fulfill(status=401, content_type="application/json", body=json.dumps({"detail": "expired"}))
            return
        elif path.endswith("/pet/chat"):
            held_pet.append(route)
            return
        elif path.endswith("/action"):
            action_requests.append(request.post_data_json)
            held_actions.append(route)
            return
        else:
            data = current
        route.fulfill(status=200, content_type="application/json", body=json.dumps(data, ensure_ascii=False))

    page.route("**/api/**", route_api)
    page.goto(BASE)
    page.locator("#createBtn").click()
    wait_condition(page, "document.querySelector('#connection').textContent.includes('实时连接')")
    assert len(session_creations) == 1, "Expired stored token was not renewed"
    assert page.locator("#uniqueModels").is_checked()
    assert "OpenAI · 未配置" in page.locator("#providerStatus").inner_text()
    assert page.locator('.bot-model option[value="openai:example"]').first.evaluate("el=>el.disabled")
    page.locator("#aiSettings").evaluate("el=>el.open=true")
    page.locator("#uniqueModels").uncheck()
    assert "兼容模式已选择" in page.locator("#modelWarning").inner_text()
    page.locator("#uniqueModels").check()
    for width in [360, 390, 768, 1280]:
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth"), f"Overflow at {width}"
    page.set_viewport_size({"width": 390, "height": 844})

    current.update(
        phase="day_speech",
        phase_name="白天发言",
        lifecycle="ACTIVE",
        turn_sequence=1,
        turn_id="turn-1",
        current_turn_player_id=2,
        state_revision=20,
    )
    current["self"].update(role="村民", role_key="villager")
    current["players"][0]["role"] = "村民"
    for seat in range(2, 7):
        current["players"].append(
            {
                "id": seat,
                "name": f"AI {seat}",
                "alive": True,
                "is_human": False,
                "is_you": False,
                "role": None,
                "provider": "dashscope",
                "personality": "detective",
                "model": "qwen-plus",
                "model_key": "dashscope:qwen-plus",
                "agent_id": f"agent-{seat}",
                "voice_profile": {
                    "voice_id": f"voice-{1 + seat % 2}",
                    "rate": 1 + seat / 100,
                    "pitch": 0.7 + seat / 10,
                },
            }
        )
    page.evaluate("s=>{window.__fixtureState=s;window.__packet({type:'state_snapshot',data:s});}", current)
    wait_condition(page, "window.__wakeRequests>0")
    page.locator("#wakeToggle").uncheck()
    wait_condition(page, "window.__wakeReleases>0")
    page.locator("#audioToggle").click()

    seq = 0
    revision = 21

    def packet(kind, data, r=None, event_id=None, turn="turn-1"):
        global seq, revision
        seq += 1
        if r is None:
            revision += 1
            r = revision
        value = {
            "type": kind,
            "data": data,
            "seq": seq,
            "event_id": event_id or f"browser-event-{seq}",
            "state_revision": r,
            "turn_id": turn,
            "game_id": current["game_id"],
        }
        page.evaluate("p=>window.__packet(p)", value)
        return value

    packet(
        "model_execution",
        {"player_id": 2, "model_used": "qwen-turbo", "provider_used": "dashscope", "status": "switched"},
    )
    page.locator("#modelAudit").evaluate("el=>el.open=true")
    assert "qwen-turbo · 已切换" in page.locator("#executionStatus").inner_text()
    packet("speech_started", {"player_id": 2})
    start = time.monotonic()
    chunk = packet("speech_chunk", {"player_id": 2, "delta": "我认为"})
    assert page.locator("#liveSpeech").inner_text() == "我认为"
    assert (time.monotonic() - start) < 0.3, "Chunk was not rendered within 300 ms"
    assert page.evaluate("window.__speech.length") == 0, "Fragments must not be read individually"
    packet("speech_chunk", {"player_id": 2, "chunk": "3号值得追问。"})
    wait_condition(page, "window.__speech.length===1")
    assert page.evaluate("window.__speech[0].lang") == "zh-CN"
    assert page.evaluate("window.__speech[0].text") == "我认为3号值得追问。"
    page.evaluate("p=>window.__packet(p)", chunk)
    assert page.locator("#liveSpeech").inner_text() == "我认为3号值得追问。", "Duplicate chunk appeared"
    speech = {
        "kind": "speech",
        "day": 1,
        "player_id": 2,
        "seq": 1,
        "text": "2号 AI 2：我认为3号值得追问。",
        "speech": "我认为3号值得追问。",
        "event_id": "public-speech-1",
    }
    packet("chat_message", speech, event_id=speech["event_id"])
    packet("speech_finished", {"player_id": 2, "speech": speech["speech"], "partial": True})
    assert page.locator("#streamStatus").is_visible()
    assert page.locator("#log .event").count() == 1
    assert page.evaluate("window.__speech.length") == 1, "Finished speech repeated a sentence"
    page.screenshot(path=str(OUT / "v22-mobile-stream.png"), full_page=True)
    packet(
        "chat_message",
        {"kind": "system", "day": 1, "seq": 2, "text": "私人身份不能被朗读", "event_id": "system-1"},
        event_id="system-1",
    )
    assert page.evaluate("window.__speech.length") == 1

    page.locator("#petOpen").click()
    page.locator("#petInput").fill("这是私人问题")
    page.locator("#petSend").click()
    wait_condition(page, "document.querySelector('#petLog').textContent.includes('搭档正在回复')")
    assert page.locator("#petLog .user").count() == 1, "Optimistic message missing"
    page.wait_for_timeout(25)
    assert held_pet, "Pet request was not submitted"
    request_id = held_pet[0].request.post_data_json["client_message_id"]
    user_entry = {
        "role": "user",
        "text": "这是私人问题",
        "time": time.time(),
        "event_id": "pet-user-1",
        "client_message_id": request_id,
    }
    reply_entry = {
        "role": "assistant",
        "text": "这是私人回复，不能默认朗读",
        "time": time.time(),
        "event_id": "pet-reply-1",
        "client_message_id": request_id,
    }
    packet("private_pet_message", user_entry, event_id=user_entry["event_id"])
    packet("private_pet_message", reply_entry, event_id=reply_entry["event_id"])
    assert page.locator("#petLog .user").count() == 1
    assert page.locator("#petLog .assistant").count() == 1
    assert page.locator("#petLog .pet-replying").count() == 0
    assert page.evaluate("window.__speech.length") == 1, "Private pet speech was read by default"
    page.screenshot(path=str(OUT / "v22-mobile-pet.png"), full_page=True)
    current["pet"]["private_chat_history"] = [user_entry, reply_entry]
    current["events"] = [
        speech,
        {"kind": "system", "day": 1, "seq": 2, "text": "私人身份不能被朗读", "event_id": "system-1"},
    ]
    current["current_speech"] = speech["speech"]
    current["state_revision"] = revision
    held_pet.pop().fulfill(status=200, content_type="application/json", body=json.dumps(current, ensure_ascii=False))
    page.wait_for_timeout(40)
    assert page.locator("#petLog .user").count() == 1 and page.locator("#petLog .assistant").count() == 1, (
        "Snapshot duplicated private messages"
    )
    page.locator("#petClose").click()

    # A stale snapshot cannot undo visible streamed speech.
    stale = copy.deepcopy(current)
    stale["state_revision"] -= 10
    stale["current_speech"] = "旧消息"
    page.evaluate("s=>window.__packet({type:'state_snapshot',data:s})", stale)
    assert page.locator("#liveSpeech").inner_text() == speech["speech"]
    before = page.evaluate("window.__audioCancels")
    packet("phase_changed", {"phase": "day_speech", "turn_id": "turn-2", "current_turn_player_id": 3}, turn="turn-2")
    assert page.evaluate("window.__audioCancels") > before
    packet("speech_started", {"player_id": 3}, turn="turn-2")
    packet("speech_chunk", {"player_id": 3, "delta": "我的观点不同。"}, turn="turn-2")
    wait_condition(page, "window.__speech.length===2")
    voices = page.evaluate("window.__speech")
    assert voices[0]["pitch"] != voices[1]["pitch"] and voices[1]["text"] == "我的观点不同。"
    page.locator("#audioToggle").click()
    packet("speech_chunk", {"player_id": 3, "delta": "这句保持静音。"}, turn="turn-2")
    assert "这句保持静音" in page.locator("#liveSpeech").inner_text()
    assert page.evaluate("window.__speech.length") == 2

    # Dead wolves retain old history but do not render new wolf messages.
    current.update(
        state_revision=revision + 1,
        phase="night_discussion",
        phase_name="狼人讨论",
        turn_id="turn-3",
        turn_sequence=3,
        current_turn_player_id=None,
        current_speech="",
    )
    revision += 1
    current["self"].update(role="狼人", role_key="wolf", alive=False)
    current["players"][0].update(role="狼人", alive=False)
    current["wolf_chat"] = [{"player_id": 2, "day": 1, "text": "旧狼聊", "seq": 1, "event_id": "wolf-old"}]
    page.evaluate("s=>window.__packet({type:'state_snapshot',data:s})", current)
    packet(
        "wolf_chat_message",
        {"player_id": 2, "day": 2, "text": "新秘密不能收到", "event_id": "wolf-new"},
        event_id="wolf-new",
        turn="turn-3",
    )
    assert "新秘密" not in page.locator("#wolfLog").inner_text()
    assert page.locator("#wolfInput").is_disabled()

    # An action double click sends exactly once and carries revision/idempotency.
    current.update(state_revision=revision + 1, phase="day_vote", phase_name="投票", turn_sequence=4, turn_id="turn-4")
    current["self"].update(role="村民", role_key="villager", alive=True)
    current["players"][0].update(role="村民", alive=True)
    current.pop("wolf_chat", None)
    current["pending_action"] = {
        "type": "vote",
        "game_id": current["game_id"],
        "turn_sequence": 4,
        "turn_id": "turn-4",
        "options": [2, 3, 4, 5, 6],
    }
    page.evaluate("s=>window.__packet({type:'state_snapshot',data:s})", current)
    page.locator("#targetInput").select_option("2")
    page.locator("#turnForm button").evaluate("b=>{b.click();b.click();}")
    page.wait_for_timeout(40)
    assert len(action_requests) == 1
    submitted = action_requests[0]
    assert (
        submitted["action_id"]
        and submitted["expected_state_revision"] == current["state_revision"]
        and submitted["turn_id"] == "turn-4"
    )
    assert page.locator("#turnForm button").is_disabled()
    page.evaluate("s=>window.__fixtureState=s", current)
    held_actions.pop().abort("failed")
    wait_condition(page, "document.querySelector('#connection').textContent.includes('实时连接')")
    page.wait_for_timeout(60)
    assert len(action_requests) == 2, "An uncertain action was not retried after reconnect"
    assert action_requests[1]["action_id"] == submitted["action_id"], "Retry changed the idempotency identifier"
    assert page.locator("#turnForm button").is_disabled()
    current["pending_action"] = None
    current["state_revision"] += 1
    held_actions.pop().fulfill(
        status=200, content_type="application/json", body=json.dumps(current, ensure_ascii=False)
    )
    page.wait_for_timeout(40)

    # Reconnect clears audio and applies the newest snapshot without replaying it.
    before = page.evaluate("window.__audioCancels")
    packet("reconnect", {}, turn="turn-4")
    assert "状态同步" in page.locator("#connection").inner_text()
    page.evaluate("s=>window.__packet({type:'state_snapshot',data:s})", current)
    assert page.locator("#log .event").count() == 2
    assert page.locator("#petLog .assistant").count() == 1
    assert page.evaluate("window.__audioCancels") > before
    page.evaluate("s=>{window.__fixtureState=s;window.dispatchEvent(new Event('offline'));}", current)
    assert "已断开" in page.locator("#connection").inner_text()
    page.evaluate("window.dispatchEvent(new Event('online'))")
    wait_condition(page, "document.querySelector('#connection').textContent.includes('实时连接')")
    previous_game = copy.deepcopy(current)
    next_game = copy.deepcopy(current)
    next_game.update(
        game_id="browser-rematch-game",
        state_revision=1,
        phase="lobby",
        phase_name="等待玩家",
        lifecycle="LOBBY",
        turn_id="next-turn-0",
        turn_sequence=0,
        events=[],
        current_speech="",
    )
    next_game["self"].update(role=None, role_key=None, private_notes=[])
    next_game["players"] = [next_game["players"][0]]
    next_game["players"][0]["role"] = None
    next_game["pet"]["private_chat_history"] = []
    page.evaluate("s=>window.__packet({type:'state_snapshot',data:s})", next_game)
    assert page.locator("#phaseText").inner_text() == "等待玩家"
    page.evaluate("s=>window.__packet({type:'state_snapshot',data:s})", previous_game)
    assert page.locator("#phaseText").inner_text() == "等待玩家", "Late previous-game snapshot replaced the rematch"
    assert page.locator("#log .event").count() == 0 and "私人回复" not in page.locator("#petLog").inner_text()
    assert page.locator("#closeRoomBtn").is_visible() and page.locator("#rematchBtn").is_hidden()
    page.screenshot(path=str(OUT / "v22-mobile-lobby.png"), full_page=True)
    flags["expire_mutation"] = True
    page.locator("#aiSettings").evaluate("el=>el.open=true")
    page.locator("#saveConfig").click()
    page.locator("#entry").wait_for(state="visible")
    assert flags["configure_calls"] == 1, "Expired-session mutation was replayed"
    assert "会话已过期" in page.locator("#notice").inner_text()
    assert not errors, errors
    print(
        json.dumps(
            {
                "browser_errors": errors,
                "widths": [360, 390, 768, 1280],
                "event_only_stream": True,
                "public_and_private_deduplication": True,
                "optimistic_pet": True,
                "tts_public_only": True,
                "tts_seat_profiles": True,
                "stale_snapshot_rejected": True,
                "old_game_snapshot_rejected": True,
                "expired_session_recovery": True,
                "expired_mutation_not_replayed": True,
                "action_double_click_requests": 1,
                "network_retry_reuses_action_id": True,
                "offline_online_reconnect": True,
                "wake_lock_release": True,
                "audibility": "instrumented WebSpeech only; real phone audio requires device validation",
            },
            ensure_ascii=False,
        )
    )
    context.close()
    browser.close()

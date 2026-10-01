"""Optional browser acceptance: start the server, then run this script (Playwright)."""

import json
import os
import time
from pathlib import Path

from browser_helpers import wait_condition
from playwright.sync_api import sync_playwright

BASE = os.getenv("TEST_BASE_URL", "http://127.0.0.1:8000")
OUT = Path(__file__).resolve().parents[1] / "test-artifacts"
OUT.mkdir(exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    events = []
    page.on(
        "websocket",
        lambda ws: ws.on(
            "framereceived", lambda frame: events.append(json.loads(frame)) if isinstance(frame, str) else None
        ),
    )
    page.goto(BASE)
    page.locator("#createBtn").click()
    page.locator("#game").wait_for(state="visible")
    wait_condition(page, "document.querySelector('#connection').textContent.includes('实时连接')")

    def get_state():
        return page.evaluate(
            "async()=>{const token=localStorage.getItem('werewolf-v2-token');const rid=localStorage.getItem('werewolf-v2-room');return (await fetch('/api/rooms/'+rid,{headers:{Authorization:'Bearer '+token}})).json();}"
        )

    room = get_state()["room_id"]
    page.screenshot(path=str(OUT / "mobile-lobby.png"), full_page=True)
    for width in [360, 390, 768, 1280]:
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
            f"Horizontal overflow at {width}"
        )
    page.set_viewport_size({"width": 390, "height": 844})
    # Another browser identity joins the same room, with a separate pet and view.
    second = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    friend = second.new_page()
    friend.goto(BASE + "/?room=" + room)
    friend.locator("#playerName").fill("朋友")
    friend.locator("#seatSelect").select_option("2")
    friend.locator("#joinBtn").click()
    friend.locator("#game").wait_for(state="visible")
    wait_condition(
        page,
        "document.querySelectorAll('#players .player').length===6 && document.querySelector('#players').textContent.includes('朋友')",
    )
    page.locator("#petOpen").click()
    page.locator("#petInput").fill("当前有哪些已知事实？")
    page.locator("#petSend").click()
    page.locator("#petLog .assistant").wait_for(state="visible")
    assert "当前有哪些已知事实" not in friend.locator("#petLog").inner_text()
    page.locator("#petPersonality").select_option("commander")
    wait_condition(page, "document.querySelector('#petPersonality').value==='commander'")
    page.screenshot(path=str(OUT / "mobile-pet.png"), full_page=True)
    page.locator("#petMode").select_option("autopilot")
    wait_condition(page, "document.querySelector('#petModeBadge').textContent==='托管'")
    page.locator("#petClose").click()
    friend.locator("#petOpen").click()
    friend.locator("#petMode").select_option("autopilot")
    wait_condition(friend, "document.querySelector('#petModeBadge').textContent==='托管'")
    friend.locator("#petClose").click()
    page.locator("#startBtn").click()
    wait_condition(page, "document.querySelector('#phaseText').textContent==='狼人讨论'")
    before = get_state()
    page.reload()
    wait_condition(page, "document.querySelector('#connection').textContent.includes('实时连接')")
    after = get_state()
    assert before["turn_deadline"] == after["turn_deadline"], "Reload changed deadline"
    assert before["game_id"] == after["game_id"]
    assert all(x["role"] is None for x in after["players"] if not x["is_you"])
    limit = time.monotonic() + 100
    screenshot = False
    while time.monotonic() < limit:
        state = get_state()
        if state["phase"] == "day_speech" and not screenshot:
            page.screenshot(path=str(OUT / "mobile-game.png"), full_page=True)
            screenshot = True
            sequence = state["turn_sequence"]
            page.locator("#petOpen").click()
            page.locator("#petInput").fill("给我一个发言草稿建议")
            page.locator("#petSend").click()
            wait_condition(page, "document.querySelector('#petSend').textContent==='发送'")
            assert get_state()["turn_sequence"] >= sequence
            page.locator("#petClose").click()
        if state["game_over"]:
            break
        page.wait_for_timeout(300)
    assert state["game_over"], "Mock game did not finish within browser acceptance time"
    page.screenshot(path=str(OUT / "mobile-finished.png"), full_page=True)
    assert not errors, errors
    assert any(e["type"] == "state_snapshot" for e in events)
    if screenshot:
        assert any(e["type"] == "speech_chunk" for e in events)
    print(
        json.dumps(
            {
                "browser_errors": errors,
                "room_id": room,
                "winner": state["winner"],
                "public_records": len(state["events"]),
                "streamed_chunks": sum(e["type"] == "speech_chunk" for e in events),
                "mobile_widths": [360, 390, 768, 1280],
            },
            ensure_ascii=False,
        )
    )
    second.close()
    context.close()
    browser.close()

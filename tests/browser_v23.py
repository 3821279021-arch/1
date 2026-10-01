"""Real browser multiplayer/session flow, with production CSP left enabled."""

import json
import os
from pathlib import Path

from browser_helpers import wait_condition
from playwright.sync_api import sync_playwright

BASE = os.getenv("TEST_BASE_URL", "http://127.0.0.1:8000")
OUT = Path(__file__).resolve().parents[1] / "test-artifacts"
OUT.mkdir(exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    host_context = browser.new_context(viewport={"width": 390, "height": 844}, service_workers="block")
    friend_context = browser.new_context(viewport={"width": 390, "height": 844}, service_workers="block")
    host = host_context.new_page()
    friend = friend_context.new_page()
    errors = []
    for page in (host, friend):
        page.on("pageerror", lambda e: errors.append(str(e)))
    host.goto(BASE)
    wait_condition(host, "document.querySelector('#connection').textContent==='选择房间'")
    code = host.locator("#recoveryDisplay").inner_text()
    assert len(code) == 24
    host.locator("#hideRecovery").click()
    host.locator("#createBtn").click()
    host.locator("#game").wait_for(state="visible")
    wait_condition(host, "document.querySelector('#connection').textContent.includes('实时连接')")
    room = host.url.split("room=")[1]
    host.locator("#roomSecurity summary").click()
    host.locator("#lockRoomBtn").click()
    wait_condition(host, "document.querySelector('#lockRoomBtn').textContent==='开放加入'")
    friend.goto(BASE + "/?room=" + room)
    friend.locator("#playerName").fill("恢复测试朋友")
    friend.locator("#seatSelect").select_option("2")
    friend.locator("#joinBtn").click()
    wait_condition(friend, "document.querySelector('#notice').textContent.includes('锁定')")
    assert friend.locator("#game").is_hidden()
    host.locator("#lockRoomBtn").click()
    wait_condition(host, "document.querySelector('#lockRoomBtn').textContent==='锁定房间'")
    host.locator("#roomPassword").fill("moon-test-password")
    host.locator("#savePasswordBtn").click()
    wait_condition(host, "document.querySelector('#passwordState').textContent==='已设置密码'")
    friend.locator("#entry .session-tools summary").click()
    friend.locator("#joinPassword").fill("moon-test-password")
    friend.locator("#joinBtn").click()
    friend.locator("#game").wait_for(state="visible")
    wait_condition(friend, "document.querySelector('#connection').textContent.includes('实时连接')")
    host.locator("#moderateSeat").select_option("2")
    host.locator("#moderateSeatBtn").click()
    friend.locator("#entry").wait_for(state="visible")
    friend.locator("#joinBtn").click()
    wait_condition(friend, "document.querySelector('#notice').textContent.includes('禁止')")
    host.locator("#reopenSeatBtn").click()
    wait_condition(host, "!document.querySelector('#reopenSeatBtn').disabled")
    friend.locator("#joinBtn").click()
    friend.locator("#game").wait_for(state="visible")
    # Clear cache/storage and restore owner identity in a third browser.
    restored_context = browser.new_context(viewport={"width": 390, "height": 844}, service_workers="block")
    restored = restored_context.new_page()
    restored.on("pageerror", lambda e: errors.append(str(e)))
    restored.goto(BASE)
    wait_condition(restored, "document.querySelector('#connection').textContent==='选择房间'")
    restored.locator("#entry .session-tools summary").click()
    restored.locator("#recoveryInput").fill(code)
    restored.locator("#recoverBtn").click()
    wait_condition(restored, "document.querySelector('#recentRooms').textContent.includes('" + room + "')")
    new_code = restored.locator("#recoveryDisplay").inner_text()
    assert new_code != code
    restored.locator("#recentRooms [data-room]").click()
    restored.locator("#game").wait_for(state="visible")
    wait_condition(restored, "document.querySelector('#connection').textContent.includes('实时连接')")
    assert restored.locator("#startBtn").is_visible()
    assert (
        "1 号" in restored.locator("#humanRole").inner_text()
        or "身份将在" in restored.locator("#humanRole").inner_text()
    )
    # Token rotation closes the old device's live connection immediately.
    wait_condition(host, "document.querySelector('#entry').offsetParent!==null")
    restored.screenshot(path=str(OUT / "v23-mobile-recovered-host.png"), full_page=True)
    assert not errors, errors
    print(
        json.dumps(
            {
                "browser_errors": errors,
                "room_id": room,
                "lock_password_kick_reopen": True,
                "cross_device_recovery": True,
                "old_socket_revoked": True,
            },
            ensure_ascii=False,
        )
    )
    browser.close()

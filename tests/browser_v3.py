"""V3 UI integration checks against a running server, keeping production CSP.

Run: TEST_BASE_URL=http://127.0.0.1:8000 python tests/browser_v3.py
Local WAV playback is real; provider catalog responses are deterministic fixtures.
No external API key is used or required.
"""

import json
import os
from pathlib import Path

from browser_helpers import wait_condition
from playwright.sync_api import sync_playwright

BASE = os.getenv("TEST_BASE_URL", "http://127.0.0.1:8000")
OUT = Path(__file__).resolve().parents[1] / "test-artifacts"
OUT.mkdir(exist_ok=True)
checks = []
errors = []


def check(value, description):
    assert value, description
    checks.append(description)


def ready(page):
    page.goto(BASE)
    wait_condition(page, "document.querySelector('#connection').textContent==='选择房间'")
    if page.locator("#recoveryNotice").is_visible():
        page.locator("#hideRecovery").click()


def viewport_check(page, count, description):
    check(page.locator(".player").count() == count, description + " avatar count")
    check(
        page.evaluate(
            "document.body.scrollHeight<=innerHeight+1 && document.documentElement.scrollWidth<=innerWidth+1"
        ),
        description + " viewport frame",
    )
    check(
        page.evaluate(
            "[...document.querySelectorAll('.player-rail')].every(rail=>[...rail.children].every((card,i,items)=>i===0||card.getBoundingClientRect().top>=items[i-1].getBoundingClientRect().bottom-1))"
        ),
        description + " avatar cards do not overlap",
    )
    check(
        page.evaluate(
            "(()=>{const p=document.querySelector('.phase').getBoundingClientRect();return p.top>=0&&p.bottom<=innerHeight})()"
        ),
        description + " visible phase",
    )
    check(
        page.evaluate(
            "(()=>{const p=document.querySelector('.game-footer').getBoundingClientRect();return p.top>=0&&p.bottom<=innerHeight})()"
        ),
        description + " visible bottom controls",
    )


with sync_playwright() as p:
    chromium = os.getenv("CHROMIUM_PATH", "/usr/bin/chromium")
    browser = p.chromium.launch(
        headless=True, executable_path=chromium if Path(chromium).exists() else None, args=["--no-sandbox"]
    )
    contexts = []

    def new_page(width=390, height=844):
        context = browser.new_context(viewport={"width": width, "height": height}, service_workers="block")
        contexts.append(context)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        return page

    host = new_page(1440, 900)
    ready(host)
    check(host.locator("#seatSelect").count() == 0, "homepage requires no seat selection")
    host.screenshot(path=str(OUT / "v3-home-desktop.png"), full_page=True)
    host.locator("#createBtn").click()
    host.locator("#game").wait_for(state="visible")
    wait_condition(host, "document.querySelector('#connection').textContent.includes('实时连接')")
    viewport_check(host, 6, "6 seats desktop")
    room = host.url.split("room=")[1]
    initial_seat = int(host.locator(".player.you .seat-number").inner_text())
    check(1 <= initial_seat <= 6, "creator automatically receives a legal random seat")
    friend = new_page()
    ready(friend)
    friend.locator("#playerName").fill("随机入座测试")
    friend.locator("#joinCode").fill(room)
    friend.locator("#joinBtn").click()
    friend.locator("#game").wait_for(state="visible")
    wait_condition(friend, "document.querySelector('#connection').textContent.includes('实时连接')")
    friend_seat = int(friend.locator(".player.you .seat-number").inner_text())
    check(friend_seat != initial_seat, "joining player automatically receives an unoccupied seat")
    friend.locator("#settingsOpen").click()
    target = next(i for i in range(1, 7) if i not in (initial_seat, friend_seat))
    friend.locator("#changeSeatSelect").select_option(str(target))
    friend.locator("#changeSeatBtn").click()
    wait_condition(friend, f"document.querySelector('.player.you .seat-number').textContent==='{target}'")
    check(True, "optional lobby seat change succeeds")
    friend.locator('[data-close="settingsDrawer"]').click()

    # Audio file playback uses the real browser media element and local PCM WAV.
    host.evaluate(
        "(()=>{window.__media=[];const original=HTMLMediaElement.prototype.play;window.__originalMediaPlay=original;HTMLMediaElement.prototype.play=function(){const item={src:this.src,resolved:false};window.__media.push(item);const result=original.call(this);result?.then(()=>item.resolved=true);return result;};})()"
    )
    host.locator("#settingsOpen").click()
    host.locator("#audioToggle").click()
    wait_condition(host, "window.__media.some(item=>item.src.endsWith('/test.wav')&&item.resolved)")
    check(True, "enabling sound plays the local test WAV")
    host.locator("#ttsToggle").check()
    wait_condition(host, "document.querySelector('#audioStatus').textContent.includes('朗读已开启')")
    host.locator("#audioToggle").click()
    check(host.locator("#ttsToggle").is_checked(), "TTS remains enabled when prompt sounds are muted")
    host.locator("#ttsRate").select_option("1.5")
    check("1.5" in host.locator("#audioStatus").inner_text(), "TTS playback rate has a separate control")
    host.locator("#ttsToggle").uncheck()
    host.evaluate(
        "(()=>{HTMLMediaElement.prototype.play=function(){return Promise.reject(new DOMException('test autoplay rejection','NotAllowedError'));};})()"
    )
    host.locator("#audioToggle").click()
    wait_condition(host, "document.querySelector('#audioStatus').textContent.includes('提示音被浏览器阻止')")
    check(True, "autoplay rejection has a visible sound error")
    host.evaluate("(()=>{HTMLMediaElement.prototype.play=window.__originalMediaPlay;})()")
    host.evaluate(
        "async()=>{const {store}=await import('/static/js/store.js');window.__originalSynth=store.synth;store.synth={getVoices:()=>[],cancel:()=>{},speak:()=>{}}}"
    )
    host.locator("#ttsToggle").check()
    wait_condition(host, "document.querySelector('#audioStatus').textContent.includes('无 voices')")
    check(True, "missing system TTS voices have a visible diagnosis")
    host.locator("#ttsToggle").uncheck()
    host.evaluate("async()=>{const {store}=await import('/static/js/store.js');store.synth=window.__originalSynth}")
    host.locator('[data-close="settingsDrawer"]').click()

    # Save/replace/delete hit real credential endpoints. Only provider discovery is
    # replaced by fixture responses; no account or paid model API is contacted.
    def unavailable_catalog(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {"verified": False, "manual_entry_allowed": True, "models": [], "error": "浏览器测试目录暂不可用"}
            ),
        )

    host.route("**/api/credentials/*/test*", unavailable_catalog)
    host.locator("#modelsOpen").click()
    wait_condition(host, "!document.querySelector('#credentialList').textContent.includes('正在')")
    host.locator("#credentialProvider").select_option("openai-compatible")
    host.locator("#credentialBase").fill("https://arena.example/v1")
    secret = "v3-browser-test-secret-1234"
    host.locator("#credentialKey").fill(secret)
    host.locator("#credentialSave").click()
    wait_condition(host, "document.querySelectorAll('[data-credential]').length===1")
    check(host.locator("#credentialKey").input_value() == "", "API key input clears after saving")
    check(
        secret not in host.locator("#credentialList").inner_text(),
        "credential center displays masks without the full API key",
    )
    credential_id = host.locator("[data-credential]").get_attribute("data-credential")
    credentials = host.evaluate(
        "async()=>{const m=await import('/static/js/store.js');return await m.runtime.credentialsAPI.list()}"
    )
    check(secret not in json.dumps(credentials), "credential response contains no plaintext key")
    host.locator('[data-close="modelDrawer"]').click()
    host.locator("#assignmentOpen").click()
    wait_condition(host, "document.querySelectorAll('.bot-model option[data-credential-id]').length>0")
    first_select = host.locator(".bot-model").first
    first_select.select_option("manual:" + credential_id)
    host.locator(".manual-model:not(.hidden) input").fill("arena-manual-model")
    host.locator("#uniqueModels").uncheck()
    host.locator("#saveConfig").click()
    wait_condition(host, "!document.querySelector('#assignmentDrawer').open")
    check(True, "BYOK manual model ID saves to an AI seat")

    # Refresh fixture demonstrates dynamic discovery in the seat selector.
    def discovered_catalog(route):
        credential = credentials["credentials"][0]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "credential": credential,
                    "verified": True,
                    "manual_entry_allowed": True,
                    "models": [{"id": "arena-discovered-model", "display_name": "动态目录测试模型"}],
                }
            ),
        )

    host.route("**/api/credentials/*/models*", discovered_catalog)
    host.locator("#modelsOpen").click()
    host.locator('[data-credential-action="models"]').click()
    wait_condition(host, "document.querySelector('#credentialList').textContent.includes('动态目录测试模型')")
    check(True, "discovered model catalog appears in API center")
    host.locator('[data-credential-action="replace"]').click()
    check(host.locator("#credentialKey").input_value() == "", "replacing credentials never refills the previous key")
    host.locator("#credentialKey").fill("v3-browser-replacement-5678")
    host.locator("#credentialSave").click()
    wait_condition(host, "document.querySelector('#credentialStatus').textContent.includes('API 已保存')")
    host.locator('[data-credential-action="delete"]').click()
    wait_condition(host, "document.querySelectorAll('[data-credential]').length===0")
    check(True, "owner can replace and delete a credential")
    host.locator('[data-close="modelDrawer"]').click()

    # Each board is a new isolated user; models remain local Mock.
    quick = None
    for mode, count, width, height in [
        ("quick6", 6, 390, 844),
        ("standard9", 9, 390, 844),
        ("standard12", 12, 375, 667),
        ("custom", 16, 375, 667),
    ]:
        page = new_page(width, height)
        ready(page)
        page.locator("#modeSelect").select_option(mode)
        if mode == "custom":
            page.locator("#customCount").select_option("16")
            page.locator('[data-role="wolf"]').fill("5")
            page.locator('[data-role="villager"]').fill("7")
        page.locator("#createBtn").click()
        page.locator("#game").wait_for(state="visible")
        wait_condition(page, "document.querySelector('#connection').textContent.includes('实时连接')")
        viewport_check(page, count, f"{count} seats {width}x{height}")
        page.screenshot(path=str(OUT / f"v3-{count}-mobile-lobby.png"), full_page=True)
        page.locator("#petOpen").click()
        page.locator("#petMode").select_option("autopilot")
        wait_condition(page, "document.querySelector('#petControlInfo').textContent.includes('托管持续')")
        page.locator("#petClose").click()
        page.locator("#startBtn").click()
        wait_condition(page, "document.body.dataset.phase!=='lobby'", timeout=30000)
        viewport_check(page, count, f"{count} seats running")
        page.screenshot(path=str(OUT / f"v3-{count}-mobile-stage.png"), full_page=True)
        if mode == "standard12":
            page.set_viewport_size({"width": 1440, "height": 900})
            viewport_check(page, count, "12 seats desktop running")
            page.screenshot(path=str(OUT / "v3-12-desktop-stage.png"), full_page=True)
            page.set_viewport_size({"width": width, "height": height})
        if mode == "quick6":
            quick = page
        if mode == "custom":
            page.set_viewport_size({"width": 844, "height": 390})
            viewport_check(page, count, "16 seats landscape 844x390")
            page.screenshot(path=str(OUT / "v3-16-landscape.png"), full_page=True)
    wait_condition(quick, "document.querySelector('#analysisOpen').offsetParent!==null", timeout=90000)
    quick.locator("#analysisOpen").click()
    wait_condition(quick, "document.querySelectorAll('.analysis-seat').length===6")
    check(True, "finished match loads six seat statistics from analysis API")
    check(quick.locator("#replayEvent").inner_text() != "等待回放记录", "finished match has public replay events")
    speech = quick.evaluate(
        "async()=>{const {store,runtime}=await import('/static/js/store.js');const speechIndex=store.replayEvents.findIndex(e=>e.type==='chat_message'&&e.data?.kind==='speech');const index=speechIndex>=0?speechIndex:store.replayEvents.findIndex(e=>e.data?.text);document.querySelector('#replaySlider').value=index;runtime.renderReplay();return store.replayEvents[index].data.text}"
    )
    check(
        speech in quick.locator("#replayEvent").inner_text(),
        "replay renders original public message from event envelope",
    )
    previous = quick.locator("#replayCounter").inner_text()
    quick.locator("#replayNext").click()
    check(previous != quick.locator("#replayCounter").inner_text(), "replay controls advance through public events")
    quick.screenshot(path=str(OUT / "v3-analysis-mobile.png"), full_page=True)
    quick.locator('[data-close="analysisDrawer"]').click()
    quick.locator("#settingsOpen").click()
    quick.locator("#rematchBtn").click()
    wait_condition(quick, "document.body.dataset.phase==='lobby'")
    quick.locator('[data-close="settingsDrawer"]').click()
    quick.locator("#historyOpen").click()
    quick.locator("#historyAnalysisOpen").click()
    wait_condition(quick, "document.querySelectorAll('.analysis-seat').length===6")
    check(True, "previous match replay remains accessible after rematch")
    check(not errors, "no JavaScript page errors")
    print(
        json.dumps(
            {
                "checks": len(checks),
                "passed": checks,
                "page_errors": errors,
                "screenshots": str(OUT),
                "provider_discovery": "fixture; credential persistence is real",
                "local_wav": "browser play() resolved",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    browser.close()

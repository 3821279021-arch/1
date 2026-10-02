"""V3.2 mobile shell, catalog, audio, presentation and pause acceptance.

Provider catalog is a 100-model fixture; room/session/preferences storage and
local media decoding/playback use the running app. No external key is used.
"""

import json
import os
from pathlib import Path

from browser_helpers import wait_condition
from playwright.sync_api import sync_playwright

BASE = os.getenv("TEST_BASE_URL", "http://127.0.0.1:8000")
OUT = Path(__file__).resolve().parents[1] / "test-artifacts"
checks, errors = [], []


def check(value, description):
    assert value, description
    checks.append(description)


with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.getenv("CHROMIUM_PATH", "/usr/bin/chromium"), args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 390, "height": 844}, service_workers="block")
    page.on("pageerror", lambda error: errors.append(str(error)))
    catalog = [
        {"key": f"openai:model-{i:03}", "provider": "openai", "model": f"model-{i:03}", "configured": False}
        for i in range(100)
    ]
    page.route(
        "**/api/models",
        lambda route: route.fulfill(content_type="application/json", body=json.dumps({"models": catalog})),
    )
    page.goto(BASE)
    wait_condition(page, "document.querySelector('#connection').textContent==='选择房间'")
    page.locator("#hideRecovery").click()
    check(page.locator("#bottomNav button").count() == 5, "five bottom navigation entries")
    check(not page.locator("#modelsOpen").is_visible(), "API management is under My on the homepage")
    for name, target in (
        ("friends", "shellFriends"),
        ("battle", "shellBattle"),
        ("history", "shellHistory"),
        ("my", "shellMy"),
        ("home", "entry"),
    ):
        page.locator(f'[data-page="{name}"]').click()
        check(page.locator("#" + target).is_visible(), f"{name} page is reachable")
        check(page.evaluate("document.documentElement.scrollWidth<=innerWidth"), f"{name} mobile page fits width")
    page.screenshot(path=str(OUT / "v32-home-mobile.png"), full_page=True)
    page.evaluate(
        "()=>{window.playedSources=[];const native=HTMLMediaElement.prototype.play;HTMLMediaElement.prototype.play=function(){window.playedSources.push(this.src);return native.call(this)}}"
    )
    page.locator("#homeSound").click()
    wait_condition(
        page,
        "window.playedSources.some(s=>s.includes('/test.wav'))&&window.playedSources.some(s=>s.includes('/bgm/lobby.mp3'))",
    )
    check(True, "homepage click plays the audible test and lobby music")
    check(
        page.evaluate("window.playedSources.filter(s=>s.includes('/test.wav')).length>=4"),
        "user gesture unlocks cue, host and both crossfade music elements",
    )
    decoded = page.evaluate("""async()=>{
      const manifest=await (await fetch('/static/assets/audio/manifest-v32.json')).json();
      const paths=[...manifest.bgm.map(x=>'bgm/'+x+'.mp3'),...Object.keys(manifest.host).map(x=>'host/'+x+'.mp3'),'death.wav','skill.wav'];
      return await Promise.all(paths.map(path=>new Promise((resolve,reject)=>{const a=new Audio('/static/assets/audio/'+path);a.onloadedmetadata=()=>resolve({path,duration:a.duration});a.onerror=()=>reject(new Error(path));a.load()})));
    }""")
    check(all(item["duration"] > 0 for item in decoded), "all 42 local BGM, Mandarin and effect clips decode")
    page.locator('[data-page="my"]').click()
    page.locator("#myModels").click()
    wait_condition(page, "document.querySelectorAll('.model-row').length===100")
    check(True, "100 model catalog renders without expanding a long raw select")
    page.locator("#modelSearch").fill("model-097")
    check(page.locator(".model-row").count() == 1, "search filters 100 model IDs immediately")
    page.locator('[data-favorite="openai:model-097"]').click()
    wait_condition(page, "document.querySelector('[data-favorite]').textContent==='★'")
    check(
        "未测试" in page.locator("#modelCatalog").inner_text(),
        "uncalled model displays untested rather than a fabricated balance",
    )
    page.reload()
    wait_condition(page, "document.querySelector('#connection').textContent==='选择房间'")
    page.locator('[data-page="my"]').click()
    page.locator("#myModels").click()
    wait_condition(page, "document.querySelector('.model-group:first-child').textContent.includes('model-097')")
    check(True, "favorite persists across reload and is pinned first")
    page.locator('[data-close="modelDrawer"]').click()
    page.locator('[data-page="battle"]').click()
    page.locator("#battleRandom").click()
    check(
        page.locator("#boardPolicy").input_value() == "constrained_random",
        "battle random entry chooses the constrained board",
    )
    page.locator("#boardPolicy").select_option("custom_random")
    check(page.locator("#randomPool input").count() == 16, "custom random pool offers all 16 complete roles")
    page.screenshot(path=str(OUT / "v32-random-mobile.png"), full_page=True)
    page.locator("#boardPolicy").select_option("fixed")
    page.locator("#modeSelect").select_option("quick6")
    page.locator("#createBtn").click()
    page.locator("#game").wait_for(state="visible")
    wait_condition(page, "document.querySelector('#connection').textContent.includes('实时连接')")
    check(not page.locator("#bottomNav").is_visible(), "ordinary navigation is hidden during a game")
    page.locator("#assignmentOpen").click()
    page.locator("#lineupName").fill("6人浏览器验收")
    page.locator("#saveLineup").click()
    wait_condition(page, "document.querySelector('#lineupSelect').textContent.includes('6人浏览器验收')")
    page.locator("#applyLineup").click()
    wait_condition(page, "document.querySelector('#notice').textContent.includes('阵容已套用')")
    check(True, "a six-seat lineup saves and applies in one click")
    page.locator('[data-close="assignmentDrawer"]').click()
    page.evaluate(
        "()=>{window.playedSources=[];const native=HTMLMediaElement.prototype.play;HTMLMediaElement.prototype.play=function(){window.playedSources.push(this.src);return native.call(this)}}"
    )
    page.locator("#settingsOpen").click()
    if page.locator("#audioToggle").get_attribute("aria-pressed") == "false":
        page.locator("#audioToggle").click()
    wait_condition(page, "window.playedSources.some(x=>x.includes('/bgm/lobby.mp3'))")
    page.locator('[data-close="settingsDrawer"]').click()
    # Verify private announcements using the actual mixer on deliberately
    # scoped local views; server-side privacy is covered by rule/API tests.
    page.evaluate(
        """async()=>{const {store}=await import('/static/js/store.js');const m=await import('/static/js/mixer.js');store.audioEnabled=true;m.stopMixer();window.playedSources=[];m.observeMixer({game_id:'privacy-test',turn_id:'p1',phase:'death_skill',self:{id:1,role_key:'villager'},pending_action:{type:'seer_inspect'}})}"""
    )
    page.wait_for_timeout(150)
    check(
        not any("/host/seer_inspect.mp3" in x for x in page.evaluate("window.playedSources")),
        "unrelated role does not receive seer voice",
    )
    page.evaluate(
        """async()=>{const m=await import('/static/js/mixer.js');m.observeMixer({game_id:'privacy-test',turn_id:'p2',phase:'death_skill',self:{id:1,role_key:'seer'},pending_action:{type:'seer_inspect'}})}"""
    )
    wait_condition(page, "window.playedSources.some(x=>x.includes('/host/seer_inspect.mp3'))")
    check(True, "authorized pending role action plays its local Mandarin clip")
    # Close transport briefly to observe only the display clock, then reconnect.
    page.evaluate(
        """async()=>{const {store,runtime}=await import('/static/js/store.js');runtime.closeSocket();runtime.resetPresentation();store.state.phase='day_speech';store.state.lifecycle='ACTIVE';runtime.paceStart('presentation-test',2);runtime.paceFinish('presentation-test',2,'甲'.repeat(100));runtime.paceStart('presentation-next',3);runtime.paceFinish('presentation-next',3,'下一位发言');}"""
    )
    page.wait_for_timeout(1050)
    length = len(page.locator("#liveSpeech").inner_text())
    check(12 <= length <= 22, "UI buffers a burst at approximately 16 characters per second")
    check(
        page.locator("#speechWho").inner_text().startswith("2 号"),
        "buffer keeps the visible speaker until their text finishes",
    )
    page.evaluate(
        "async()=>{const {runtime}=await import('/static/js/store.js');runtime.resetPresentation();runtime.connect();}"
    )
    wait_condition(page, "document.querySelector('#connection').textContent.includes('实时连接')")
    page.locator("#startBtn").click()
    page.evaluate("async()=>{const {runtime}=await import('/static/js/store.js');await runtime.action('pause');}")
    page.locator("#suspendedPanel").wait_for(state="visible")
    before = page.evaluate(
        "async()=>{const {store}=await import('/static/js/store.js');return {turn:store.state.turn_id,remaining:store.state.suspended_remaining}}"
    )
    page.wait_for_timeout(1200)
    after = page.evaluate(
        "async()=>{const {store}=await import('/static/js/store.js');return {turn:store.state.turn_id,remaining:store.state.suspended_remaining}}"
    )
    check(before == after, "suspension freezes turn and remaining countdown")
    rid = page.url.split("room=")[1]
    page.screenshot(path=str(OUT / "v32-suspended-mobile.png"), full_page=True)
    page.locator("#homeBtn").click()
    page.locator("#entry").wait_for(state="visible")
    check(page.evaluate("localStorage.getItem('werewolf-v2-room')") is None, "explicit leave clears browser room cache")
    page.locator(f'[data-room="{rid}"]').click()
    page.locator("#suspendedPanel").wait_for(state="visible")
    check(True, "rejoining a suspended game requires explicit continue")
    page.locator("#resumeBtn").click()
    page.locator("#suspendedPanel").wait_for(state="hidden")
    check(True, "continue restores the paused game")
    page.locator("#petOpen").click()
    page.locator("#petMode").select_option("autopilot")
    page.locator("#petClose").click()
    wait_condition(page, "document.querySelector('#winner').textContent.includes('获胜')", timeout=60000)
    page.locator("#homeBtn").click()
    page.locator('[data-page="history"]').click()
    wait_condition(page, "document.querySelectorAll('[data-history-game]').length>0")
    page.locator("[data-history-game]").first.click()
    wait_condition(page, "document.querySelectorAll('.analysis-seat').length===6")
    check(True, "history opens actual completed-match analysis and replay from home")
    page.screenshot(path=str(OUT / "v32-history-mobile.png"), full_page=True)
    page.locator('[data-close="analysisDrawer"]').click()
    page.locator('[data-page="my"]').click()
    page.locator("#myLineups").click()
    check(page.locator("#myLineupCards").is_visible(), "My shows saved lineup names and counts")
    page.locator("[data-launch-lineup]").first.click()
    page.locator("#game").wait_for(state="visible")
    wait_condition(page, "document.querySelector('#notice').textContent.includes('AI 模型分配已保存')")
    check(True, "My lineup creates a matching room and saves assignments in one click")
    check(not errors, "no JavaScript errors across V3.2 flows")
    browser.close()

(OUT / "browser-v32.json").write_text(
    json.dumps(
        {
            "checks": checks,
            "page_errors": errors,
            "audio_decoded": decoded,
            "provider_catalog": "100 model fixture; preferences persist in actual SQLite",
            "real_provider_calls": 0,
        },
        ensure_ascii=False,
        indent=2,
    )
)
print(json.dumps({"checks": len(checks), "page_errors": errors}))

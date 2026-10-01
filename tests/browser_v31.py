"""Real browser import/filter/error/XSS checks for the standalone dashboard."""

import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = os.getenv("TEST_BASE_URL", "http://127.0.0.1:8000")
SUMMARY = ROOT / "test-artifacts/arena/browser-fixture/summary.json"
checks = []

with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 390, "height": 844})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/benchmark")
    assert page.title() == "AI 狼人杀 · Benchmark"
    page.locator("#files").set_input_files(str(SUMMARY))
    page.locator("#models tr").first.wait_for()
    checks.append("summary import renders models, N and 95% intervals")
    assert page.locator("#games tr").count() == 2
    checks.append("both completed games are visible")
    page.locator("#filter-faction").select_option("wolves")
    assert page.locator("#models").inner_text().count("good") == 0
    checks.append("faction filter excludes good observations")
    page.locator("#filter-seat").select_option("1")
    assert "1:" in page.locator("#models").inner_text() or page.locator("#models tr").count() == 0
    checks.append("seat filter can reduce sample size to zero")
    data = json.loads(SUMMARY.read_text())
    data["game_records"][0]["game_seed"] = 18446744073709551615
    data["observations"][0]["model"] = '<img src=x onerror="window.injected=true">'
    page.locator("#files").set_input_files(
        {"name": "summary.json", "mimeType": "application/json", "buffer": json.dumps(data).encode()}
    )
    page.locator("#filter-faction").select_option("")
    page.wait_for_function("document.querySelector('#games tr td:nth-child(3)').textContent==='18446744073709551615'")
    checks.append("64-bit seed renders exactly without JavaScript integer rounding")
    assert page.locator("#models img").count() == 0
    assert not page.evaluate("Boolean(window.injected)")
    checks.append("untrusted artifact strings are rendered as text")
    page.locator("#files").set_input_files({"name": "invalid.json", "mimeType": "application/json", "buffer": b"{}"})
    page.wait_for_function("document.getElementById('message').textContent.includes('导入失败')")
    checks.append("bad schema shows an actionable import error")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    checks.append("mobile page has no horizontal overflow outside table scroll")
    assert errors == [], errors
    checks.append("no browser script errors")
    page.screenshot(path=str(ROOT / "test-artifacts/v31-benchmark-mobile.png"), full_page=True)
    browser.close()

(ROOT / "test-artifacts/browser-v31.json").write_text(json.dumps({"checks": checks, "page_errors": errors}, indent=2))
print(json.dumps({"checks": len(checks), "page_errors": errors}))

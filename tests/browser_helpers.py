"""Poll through DevTools evaluation without weakening the page's CSP."""

import time


def wait_condition(page, expression, timeout=30_000):
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        if page.evaluate(expression):
            return
        page.wait_for_timeout(50)
    raise AssertionError("Browser condition timed out: " + expression)

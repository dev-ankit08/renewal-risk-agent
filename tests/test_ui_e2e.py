"""
Browser end-to-end test of the admin UI (Playwright + Chromium), against the
real FastAPI app on the CSV demo with the fake model client:

  first-run setup -> connect Stripe (token) -> map a CSV source's fields with
  AI suggestions -> preview -> save -> approve an account match -> signals ->
  access -> health -> ask a question on the public page with an API key.

Skipped when the UI is not built (`npm run build` in web/) or Chromium is not
installed (`python -m playwright install chromium`).
"""
import socket
import threading
import time
from pathlib import Path

import pytest

UI_BUILT = (Path(__file__).resolve().parent.parent / "renewal_risk" / "admin" / "static" / "index.html").exists()
pytestmark = pytest.mark.skipif(not UI_BUILT, reason="admin UI not built")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server(ctx):
    uvicorn = pytest.importorskip("uvicorn")
    from renewal_risk.service.api import app

    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium not installed: {exc}")
        pg = browser.new_page()
        pg.set_default_timeout(15000)
        yield pg
        browser.close()


def test_admin_ui_end_to_end(server, page, ctx, demo_config, monkeypatch):
    from renewal_risk.admin.api import secret_store_for
    from renewal_risk.admin.auth import setup_token

    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    token = setup_token(secret_store_for(ctx), ctx.store)

    # 1. First run: pick the business type, create the admin.
    page.goto(f"{server}/admin/")
    page.get_by_text("What kind of business is this?").wait_for()
    page.get_by_text("IT services & consulting").wait_for()  # both business types are offered
    page.locator("input[type=radio][value=saas]").check()
    page.locator("form input").nth(2).fill(token)
    page.locator("input[type=email]").fill("admin@demo.example")
    page.locator("input[type=password]").fill("long-enough-pw")
    page.get_by_role("button", name="Create admin & continue").click()

    # 2. Data sources: categories from the SaaS pack, connect Stripe with a token.
    page.get_by_role("heading", name="Data sources").wait_for()
    billing = page.locator(".card", has=page.get_by_role("heading", name="Billing & payments"))
    billing.get_by_role("button", name="+ Connect a system").click()
    modal = page.get_by_role("dialog")
    modal.locator("select").first.select_option("stripe")
    modal.locator("input").first.fill("payments")
    modal.locator("input[type=password]").fill("rk_test_e2e")
    modal.get_by_role("button", name="Save & test").click()
    billing.get_by_text("payments", exact=True).wait_for()
    assert "rk_test_e2e" not in (demo_config / "connectors.yaml").read_text(encoding="utf-8")

    # 3. Upload a CSV for support and map it with suggestions.
    support = page.locator(".card", has=page.get_by_role("heading", name="Support"))
    support.get_by_role("button", name="+ Connect a system").click()
    modal = page.get_by_role("dialog")
    modal.locator("select").first.select_option("csv")
    modal.locator("input").first.fill("legacy")
    csv_file = demo_config.parent / "tickets.csv"
    csv_file.write_text("TicketNo,Org,State,Severity,Opened\nT1,A001,Open,High,2026-01-05\nT2,A002,Closed,Low,2026-01-06\n", encoding="utf-8")
    modal.locator("input[type=file]").set_input_files(str(csv_file))
    modal.get_by_role("button", name="Upload").click()
    modal.get_by_text("What is in this file?").wait_for()
    modal.locator("select").nth(2).select_option("Org")
    modal.get_by_role("button", name="Continue to field mapping").click()
    page.get_by_role("heading", name="ticket from legacy").wait_for()
    page.get_by_role("button", name="Suggest with AI").click()
    page.get_by_text("fake model").first.wait_for()
    page.get_by_role("button", name="Preview").click()
    page.get_by_text("how legacy records will look").wait_for()
    page.get_by_role("button", name="Save mapping").click()
    page.get_by_text("Saved", exact=False).first.wait_for()
    assert (demo_config / "mappings" / "legacy.overrides.yaml").exists()

    # 4. Account matching: approve the suggested Stark match.
    page.get_by_role("link", name="Account matching").click()
    page.get_by_text("Suggested matches to review").wait_for()
    page.get_by_role("button", name="Match", exact=True).first.click()
    page.get_by_text("83% matched").wait_for()

    # 5. Signals, access, health render and save.
    page.get_by_role("link", name="Signals & playbooks").click()
    page.get_by_role("button", name="Save signals").click()
    page.get_by_text("Saved", exact=False).first.wait_for()
    page.get_by_role("link", name="Access & Slack").click()
    page.get_by_role("heading", name="People").wait_for()
    page.get_by_role("link", name="Health & feedback").click()
    page.get_by_role("heading", name="Recent assessments — was the agent right?").wait_for()

    # 6. The public ask page answers with an API key. The uploaded CSV is not marked as test
    #    data, so the data-retention gate must be confirmed first (it refuses otherwise).
    monkeypatch.setenv("RENEWAL_AGENT_ZDR_CONFIRMED", "true")
    page.goto(f"{server}/")
    page.locator("#key").fill("demo-key-change-me")
    page.locator("#q").fill("is Acme Corp at risk")
    page.locator("#ask-btn").click()
    page.locator("#results > *").first.wait_for()
    assert page.get_by_text("Recommended actions").count(), page.locator("#results").inner_text()
    assert page.locator(".badge.high").count() >= 1
    assert errors == [], errors

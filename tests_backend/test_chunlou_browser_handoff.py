"""A legacy bookmark must recover when the current Chunlou client uses its load-balancer key."""

import json
from pathlib import Path
from urllib.parse import quote

import pytest
from playwright.async_api import async_playwright, expect

from backend.app import create_app
from backend.config import Settings
from scripts.automation_acceptance import serve
from tests_backend.test_app import Policy
from tests_backend.test_chunlou_account_import import ISSUER, PROJECT, mock_identity, payload

CURRENT_KEY = f"sb-{PROJECT}-all-auth-token"
LEGACY_BOOKMARK = Path(__file__).with_name("fixtures") / "chunlou-legacy-bookmark.js"


async def run_bookmark(source, url):
    link = source.locator("#bookmark-fixture")
    await link.evaluate("(a, href) => a.setAttribute('href', href)", url)
    async with source.expect_popup() as opened:
        await link.click()
    receiver = await opened.value
    await expect(receiver.locator("#confirm-save")).to_be_enabled()
    return receiver


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
async def test_current_account_recovered_after_legacy_bookmark_misses_key(tmp_path, monkeypatch, engine):
    app = create_app(Settings(data_dir=tmp_path, admin_token=""), policy=Policy())
    calls = mock_identity(monkeypatch)
    fixture_session = payload(key=CURRENT_KEY)["session"]
    state_path = app.state.browser._state_path("chunlou")
    async with serve(app) as base, async_playwright() as pw:
        browser = await getattr(pw, engine).launch()
        try:
            ctx = await browser.new_context(service_workers="block")
            await ctx.route("https://chunlou.tv/**", lambda r: r.fulfill(
                content_type="text/html", body='<a id="bookmark-fixture">Run bookmark</a>'
            ))
            imports = []
            ctx.on("request", lambda r: imports.append(r) if r.url.endswith("/local-auth/import") else None)
            source = await ctx.new_page()
            await source.goto("https://chunlou.tv/feed")
            await source.evaluate("([key, value]) => localStorage.setItem(key, value)", [CURRENT_KEY, fixture_session])
            # Use the actual frozen bookmark distributed in the previous release.
            old_code = LEGACY_BOOKMARK.read_text().strip().replace("http://127.0.0.1:4173", base)
            old_page = await run_bookmark(source, "javascript:" + quote(old_code, safe=""))
            await old_page.locator("#confirm-save").click()
            await expect(old_page.locator("#status")).to_have_attribute("data-state", "error")
            await expect(old_page.locator("#return-source")).to_be_visible()
            update = old_page.locator("#update-bookmark")
            await expect(update).to_be_visible()
            install_link = await update.evaluate("a => a.href")
            assert install_link == base + "/chunlou-connect.html?site=chunlou#manual-update"
            await expect(old_page.locator("#restart")).to_be_hidden()
            assert imports == [] and calls == [] and not state_path.exists()

            async with old_page.expect_event("close"):
                await old_page.locator("#return-source").click()
            assert not source.is_closed()
            installer = await ctx.new_page()
            await installer.goto(install_link)
            await expect(installer.locator("#open-site")).to_be_enabled()
            await expect(installer.locator("#bookmark-address")).to_be_visible()
            current = await installer.locator("#bookmark-address").input_value()
            assert current == await installer.locator("#bookmark").get_attribute("href")
            receiver = await run_bookmark(source, current)
            assert imports == [] and calls == [] and not state_path.exists()
            async with receiver.expect_response(lambda r: r.url.endswith("/local-auth/import")) as post:
                await receiver.locator("#confirm-save").click()
            assert (await post.value).status == 200
            await expect(receiver.locator("#status")).to_have_attribute("data-state", "saved")
            assert [r.post_data_json["storageKey"] for r in imports] == [CURRENT_KEY]
            assert calls.count(ISSUER + "/user") == 1
            saved = app.state.browser._saved_state("chunlou")
            (entry,) = saved["origins"][0]["localStorage"]
            assert entry["name"] == CURRENT_KEY and json.loads(entry["value"])["user"]["id"] == "fixture-user"
            assert b"fixture-secret-refresh" not in state_path.read_bytes()
        finally:
            await browser.close()

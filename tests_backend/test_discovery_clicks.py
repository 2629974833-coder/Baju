"""Exercise current overview/series remove controls during background refresh."""

from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright, expect

DIST = Path(__file__).resolve().parents[1] / "dist"


@pytest.fixture
async def workbench():
    scan = {
        "id": "first", "siteId": "site", "status": "completed",
        "url": "https://example.invalid/show",
        "catalog": {"seriesKey": "show", "entries": [
            {"ordinal": 1, "title": "第 1 集", "episodeKey": "show:1"}
        ]},
    }
    state = {
        "mode": "real", "version": 1, "sites": [], "jobs": [],
        "assets": [{"id": "image", "title": "样例", "siteId": "site", "kind": "image"}],
        "scans": [scan, {**scan, "id": "second"},
                  {**scan, "id": "single", "catalog": {**scan["catalog"], "title": "独立扫描"}}],
        "series": [{"id": "group", "siteId": "site", "title": "同一部剧",
                    "sourceScanIds": ["first", "second"], "scanId": "first",
                    "catalog": scan["catalog"]}],
    }

    async def serve(route):
        path = urlsplit(route.request.url).path
        if path == "/api/capabilities":
            return await route.fulfill(json={"mode": "real", "version": 1})
        if path == "/api/state":
            return await route.fulfill(json=state)
        source = DIST / (path.lstrip("/") or "index.html")
        if source.is_file():
            return await route.fulfill(path=source)
        return await route.fulfill(status=404)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.route("**/*", serve)
        await page.clock.install()
        await page.goto("http://framekeep.test/#discover")
        # The old discovery URL remains a valid entry point into the overview.
        await expect(page.get_by_role("heading", name="拾取链接", exact=True)).to_be_visible()
        await expect(page).to_have_url("http://framekeep.test/#home")
        yield page, state
        await browser.close()


async def prepare_remove(page, control):
    if control == "移除所选":
        await page.locator("[data-home-pending] > summary").click()
        await page.get_by_role("checkbox", name="选择 样例", exact=True).check()
    else:
        await page.get_by_role("button", name="全部剧集", exact=True).click()
        if control == "移除所选整剧":
            await page.get_by_role("checkbox", name="选择整部剧：同一部剧", exact=True).check()
        else:
            select = page.get_by_role("button", name="全选当前结果", exact=True)
            if not await select.is_visible():
                await page.locator("details[data-series-catalog] > summary").click()
            await select.click()
    return page.get_by_role("button", name=control, exact=True)


@pytest.mark.parametrize(("control", "gesture", "confirm"), [
    ("移除所选单集", "mouse", "确认移除"),
    ("移除所选整剧", "space", "确认移除"),
    ("移除所选", "mouse", "确认删除"),
])
async def test_poll_between_press_and_release_still_opens_confirmation(workbench, control, gesture, confirm):
    page, state = workbench
    remove = await prepare_remove(page, control)
    if gesture == "mouse":
        await remove.hover()
        await page.mouse.down()
    else:
        await remove.focus()
        await page.keyboard.down("Space")

    # The queue counter updates outside the result list even if list rendering is deferred.
    state["jobs"] = [{"id": "job", "status": "running"}]
    await page.clock.run_for(1500)
    await expect(page.locator('[data-nav-count="queue"]')).to_have_text("1")

    if gesture == "mouse":
        await page.mouse.up()
    else:
        await page.keyboard.up("Space")
    await expect(page.get_by_role("button", name=confirm)).to_be_visible(timeout=1500)


async def test_abandoned_press_allows_the_next_poll_to_render(workbench):
    page, state = workbench
    remove = await prepare_remove(page, "移除所选")
    await remove.hover()
    await page.mouse.down()
    await page.mouse.move(5, 5)
    await page.mouse.up()
    state["assets"][0]["title"] = "更新后的素材"
    await page.clock.run_for(1500)
    await expect(page.get_by_role("button", name="更新后的素材", exact=True)).to_be_visible(timeout=1500)
    await expect(page.get_by_role("dialog")).to_have_count(0)

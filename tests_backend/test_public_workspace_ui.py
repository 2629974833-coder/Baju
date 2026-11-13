"""Public workspace presentation uses only a synthetic visitor's mocked records."""
import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import async_playwright, expect

DIST = Path(__file__).resolve().parents[1] / "dist"


@asynccontextmanager
async def panel(*, public=True, route="home", flag_in_state=False, mobile=False):
    asset = {"id": "own-video", "title": "我的测试视频", "siteId": "site", "kind": "video", "status": "downloaded", "sizeBytes": 1000, "width": 320, "height": 240, "fileUrl": "/api/assets/own-video/file"}
    state = {"mode": "real", "version": 1, "sites": [{"id": "site", "name": "测试来源", "url": "https://example.org", "domain": "example.org"}], "assets": [asset], "jobs": [], "series": [], "scans": []}
    if flag_in_state:
        state.update(publicMode=public, retentionHours=24)
    caps = {"mode": "real", "version": 1, "intakeScopes": ["site", "series", "single"]}
    if not flag_in_state:
        caps.update(publicMode=public, retentionHours=24)

    async def serve(intercept):
        path = urlsplit(intercept.request.url).path
        if path == "/api/capabilities":
            return await intercept.fulfill(json=caps)
        if path == "/api/state":
            return await intercept.fulfill(json=state)
        if path == "/api/sites/site/session":
            return await intercept.fulfill(json={"url": "https://example.org", "title": "测试登录窗口", "tabs": []})
        if path == "/api/sites/site/session/screenshot":
            return await intercept.fulfill(content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200"></svg>')
        if path == "/api/library/archive-file":
            assert intercept.request.method == "POST"
            fields = parse_qs(intercept.request.post_data)
            assert json.loads(fields["payload"][0]) == {"ids": ["own-video"], "variant": "original"}
            return await intercept.fulfill(body=b"PK\x05\x06" + b"\x00" * 18, headers={"Content-Type": "application/zip", "Content-Disposition": 'attachment; filename="my-originals.zip"'})
        source = DIST / (path.lstrip("/") or "index.html")
        if source.is_file():
            return await intercept.fulfill(path=source)
        return await intercept.fulfill(status=404)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": 390, "height": 844} if mobile else {"width": 1440, "height": 1000})
        page = await context.new_page()
        await page.route("**/*", serve)
        await page.goto(f"http://framekeep.test/#{route}")
        await expect(page.locator("#page-content")).to_be_visible()
        try:
            yield page
        finally:
            await browser.close()


@pytest.mark.parametrize("route", ["process", "automation", "settings"])
async def test_public_admin_routes_return_to_download_home(route):
    async with panel(route=route) as page:
        await expect(page).to_have_url("http://framekeep.test/#home")
        await expect(page.locator("[data-workspace-notice]")).to_contain_text("24 小时未使用后清理")
        await expect(page.locator('[data-service-status]')).to_have_text("下载服务在线")
        await expect(page.locator("#page-content")).not_to_contain_text("开始处理后")
        for name in ["工作室", "自动化", "系统"]:
            await expect(page.get_by_role("navigation").get_by_role("button", name=name, exact=True)).to_have_count(0)
        await expect(page.get_by_role("button", name="单集或短视频", exact=True)).to_have_attribute("aria-pressed", "true")


async def test_public_saved_file_detail_and_batch_export_have_no_processing_actions():
    async with panel(route="library", flag_in_state=True) as page:
        await expect(page.get_by_role("heading", name="资料库", exact=True)).to_be_visible()
        await expect(page.locator("#page-content")).not_to_contain_text("成品 ZIP")
        await expect(page.locator('[data-action="process-selection"], [data-page="process"]')).to_have_count(0)
        await page.get_by_role("checkbox", name="选择 我的测试视频", exact=True).check()
        await expect(page.locator("#batch-root")).to_contain_text("保存所选原件")
        await expect(page.locator('[data-action="export-processed"]')).to_have_count(0)
        async with page.expect_download() as pending:
            await page.locator("#batch-root").get_by_role("button", name="保存所选原件（ZIP）", exact=True).click()
        downloaded = await pending.value
        assert downloaded.suggested_filename == "my-originals.zip"
        assert downloaded.url.endswith("/api/library/archive-file")
        await page.get_by_role("button", name="我的测试视频", exact=True).click()
        await expect(page.locator('.inspector-actions a[download]')).to_contain_text("保存到本机")
        await page.get_by_role("button", name="完整详情", exact=True).click()
        await expect(page.locator('#overlay-root a[download]')).to_have_attribute("href", "/api/assets/own-video/file?download=true")
        await expect(page.locator('#overlay-root a[download]')).to_contain_text("保存到本机")
        await expect(page.locator('#overlay-root [data-action="process-asset"]')).to_have_count(0)
        await expect(page.locator("#overlay-root")).not_to_contain_text("原件始终保留")


async def test_public_mobile_notice_and_help_fit_without_server_controls():
    async with panel(mobile=True) as page:
        await expect(page.locator("[data-workspace-notice]")).to_be_visible()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.get_by_role("button", name="打开导航", exact=True).click()
        await page.get_by_role("button", name="使用指南", exact=True).click()
        await expect(page.locator("#overlay-root")).to_contain_text("保存到本机")
        await expect(page.locator("#overlay-root")).not_to_contain_text("运行维护")
        await expect(page.locator("#overlay-root")).not_to_contain_text("CPU")


async def test_public_source_authorization_does_not_request_desktop_browser_controls():
    async with panel(route="sites") as page:
        requested = []
        page.on("request", lambda request: requested.append(request.url))
        await page.get_by_role("button", name="站点授权", exact=True).click()
        await expect(page.locator("#overlay-root")).to_contain_text("会话仅供当前临时空间使用")
        await expect(page.get_by_role("button", name="打开登录窗口", exact=True)).to_be_visible()
        await expect(page.locator("#overlay-root")).not_to_contain_text("本机浏览器")
        await page.get_by_role("button", name="打开登录窗口", exact=True).click()
        await expect(page.locator("#remote-title")).to_have_text("测试登录窗口")
        await expect(page.get_by_role("button", name="使用本机浏览器授权", exact=True)).not_to_be_visible()
        assert not any("/local-auth" in url for url in requested)


async def test_local_ui_retains_studio_and_system():
    async with panel(public=False, route="home") as page:
        await expect(page.locator("[data-workspace-notice]")).to_have_count(0)
        await expect(page.locator('[data-service-status]')).to_have_text("本机服务在线")
        for name in ["工作室", "自动化", "系统"]:
            await expect(page.get_by_role("navigation").get_by_role("button", name=name, exact=True)).to_be_visible()
        await expect(page.get_by_role("button", name="整站", exact=True)).to_have_attribute("aria-pressed", "true")

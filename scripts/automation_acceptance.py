"""Verify automation using a disposable workspace, actual Chromium and two loopback HTTP servers.

Run: uv run python scripts/automation_acceptance.py
Optional UI screenshots: --screenshots test-output/automation
"""

import argparse
import asyncio
import hashlib
import io
import json
import socket
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, Response
from PIL import Image
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app import create_app  # noqa: E402
from backend.config import Settings  # noqa: E402
from backend.store import Store  # noqa: E402
from scripts.download_acceptance import inspect_and_decode, media_command  # noqa: E402


def png(color):
    buffer = io.BytesIO()
    Image.new("RGB", (320, 200), color).save(buffer, format="PNG")
    return buffer.getvalue()


class Gallery:
    def __init__(self):
        self.images = {"fern": png("#327057")}
        self.locked = False
        self.read_requests = 0
        self.write_requests = 0
        self.app = FastAPI()

        @self.app.get("/", response_class=HTMLResponse)
        async def gallery():
            if self.locked:
                return '<h1>Sign in</h1><input type="password" aria-label="Password">'
            pictures = "".join(
                f'<img src="/{name}.png" width="320" height="200" alt="{name}">'
                for name in self.images
            )
            probe = """<script>fetch('/read-probe'); fetch('/write-probe', {method:'POST'}).catch(() => {})</script>"""
            return "<title>Garden field notes</title><h1>Observations</h1>" + pictures + probe

        @self.app.get("/read-probe")
        async def read_probe():
            self.read_requests += 1
            return Response(status_code=204)

        @self.app.post("/write-probe")
        async def write_probe():
            self.write_requests += 1
            return Response(status_code=204)

        @self.app.get("/{name}.png")
        async def photo(name: str):
            if self.locked or name not in self.images:
                return Response(status_code=403)
            return Response(self.images[name], media_type="image/png")


@asynccontextmanager
async def serve(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(20):
                while not server.started:
                    if task.done():
                        await task
                        raise AssertionError("临时服务在启动期间停止")
                    await asyncio.sleep(0.05)
            yield origin
        finally:
            server.should_exit = True
            await task


async def wait_for(client, rule_id, scans, status, jobs):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        state = (await client.get("/api/state")).raise_for_status().json()
        rule = next(r for r in state["rules"] if r["id"] == rule_id)
        if len(state["scans"]) >= scans and rule["status"] not in {"idle", "running"}:
            assert len(state["scans"]) == scans, state["scans"]
            assert rule["status"] == status, rule
            assert len(state["jobs"]) == jobs, state["jobs"]
            if all(j["status"] not in {"running", "queued"} for j in state["jobs"]):
                assert all(j["status"] == "completed" for j in state["jobs"]), state["jobs"]
                return state, rule
        await asyncio.sleep(0.2)
    raise AssertionError({"rule": rule, "scans": state["scans"], "jobs": state["jobs"]})


async def click_request(page, locator, method, path):
    async with page.expect_response(
        lambda r: r.request.method == method and httpx.URL(r.url).path == path
    ) as pending:
        await locator.click()
    response = await pending.value
    assert response.ok, (response.status, await response.text())
    return response


async def match_originals(client, state, expected_images):
    actual_hashes = set()
    for asset in state["assets"]:
        content = (await client.get(asset["fileUrl"])).raise_for_status().content
        actual_hash = hashlib.sha256(content).hexdigest()
        assert actual_hash == asset["sha256"]
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
        actual_hashes.add(actual_hash)
    expected_hashes = {hashlib.sha256(image).hexdigest() for image in expected_images}
    assert len(state["assets"]) == len(expected_hashes) and actual_hashes == expected_hashes


async def verify_video_selection(root):
    root.mkdir()
    files = {}
    for name, color, pitch, duration in [
        ("episode-1", "teal", 440, 4),
        ("trailer", "navy", 220, 1),
    ]:
        path = root / f"{name}.mp4"
        await asyncio.to_thread(
            media_command,
            [
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=160x90:r=10:d={duration}",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={pitch}:duration={duration}",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                "-shortest",
                str(path),
            ],
        )
        files[name] = path
    source_app = FastAPI()

    @source_app.get("/", response_class=HTMLResponse)
    async def show():
        clips = "".join(
            f'<video controls preload="none" src="/{name}.mp4"></video>' for name in files
        )
        return "<title>Garden video observation</title><h1>Short course</h1>" + clips

    @source_app.get("/{name}.mp4")
    async def clip(name: str):
        if name not in files:
            return Response(status_code=404)
        return FileResponse(files[name], media_type="video/mp4")

    settings = Settings(
        data_dir=root / "workspace", allow_private_hosts={"127.0.0.1"}, admin_token=""
    )
    async with serve(source_app) as source, serve(create_app(settings)) as panel:
        async with httpx.AsyncClient(base_url=panel, trust_env=False) as client:
            site = (
                (await client.post("/api/sites", json={"name": "花园观察课程", "url": source}))
                .raise_for_status()
                .json()
            )
            rule = (
                (
                    await client.post(
                        "/api/rules",
                        json={
                            "name": "花园课程正片",
                            "siteId": site["id"],
                            "url": source,
                            "kinds": ["video"],
                            "autoDownload": True,
                        },
                    )
                )
                .raise_for_status()
                .json()
            )
            (await client.post(f"/api/rules/{rule['id']}/run")).raise_for_status()
            state, rule = await wait_for(client, rule["id"], 1, "completed", 1)
            assert (rule["lastNewCount"], rule["lastQueuedCount"]) == (2, 1), rule
            by_role = {asset["classification"]["kind"]: asset for asset in state["assets"]}
            assert set(by_role) == {"episode", "preview"}
            episode, preview = by_role["episode"], by_role["preview"]
            assert (
                preview["status"] == "discovered" and state["jobs"][0]["assetId"] == episode["id"]
            )
            content = (await client.get(episode["fileUrl"])).raise_for_status().content
            expected = hashlib.sha256(files["episode-1"].read_bytes()).hexdigest()
            assert hashlib.sha256(content).hexdigest() == expected == episode["sha256"]
            exported = root / "export.mp4"
            exported.write_bytes(content)
            decoded = await asyncio.to_thread(inspect_and_decode, exported)
            assert abs(decoded["durationSeconds"] - 4) < 0.1, decoded
            return {
                "discovered": 2,
                "queuedEpisodes": 1,
                "queuedPreviews": 0,
                "bytes": len(content),
                "sha256": expected,
                "fullDecode": decoded["fullDecode"],
                "durationSeconds": decoded["durationSeconds"],
            }


async def verify(data_dir, screenshots):
    settings = Settings(data_dir=data_dir, allow_private_hosts={"127.0.0.1"}, admin_token="")
    gallery = Gallery()
    errors = []
    new_per_run = []
    async with serve(gallery.app) as source, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 920}, locale="zh-CN")
        page.set_default_timeout(15000)
        page.on("pageerror", lambda error: errors.append(str(error)))
        async with (
            serve(create_app(settings)) as panel,
            httpx.AsyncClient(base_url=panel, trust_env=False) as client,
        ):
            site = (
                (await client.post("/api/sites", json={"name": "本地花园", "url": source}))
                .raise_for_status()
                .json()
            )
            await page.goto(panel + "/#automation")
            await page.locator("[data-action=new-rule]").first.click()
            form = page.locator("#rule-form")
            await form.locator("[name=name]").fill("本地花园每日更新")
            await form.locator("[name=siteId]").select_option(site["id"])
            await form.locator("[name=url]").fill(source + "/")
            await form.locator("[name=kinds][value=image]").check()
            await form.locator("[name=kinds][value=video]").uncheck()
            await form.locator("[name=enabled]").check()
            await form.locator("[name=autoDownload]").check()
            created = await click_request(page, form.locator("[type=submit]"), "POST", "/api/rules")
            rule_id = (await created.json())["id"]
            path = f"/api/rules/{rule_id}"
            row = page.locator(f'[data-rule-id="{rule_id}"]')
            run = row.locator("[data-action=run-rule]")
            await click_request(page, run, "POST", path + "/run")
            state, rule = await wait_for(client, rule_id, 1, "completed", 1)
            assert (rule["lastNewCount"], rule["lastQueuedCount"]) == (1, 1), rule
            new_per_run.append(rule["lastNewCount"])
            await match_originals(client, state, gallery.images.values())

            await page.set_viewport_size({"width": 390, "height": 844})
            await click_request(page, run, "POST", path + "/run")
            _, rule = await wait_for(client, rule_id, 2, "completed", 1)
            assert (rule["lastNewCount"], rule["lastQueuedCount"]) == (0, 0), rule
            new_per_run.append(rule["lastNewCount"])
            await page.reload()
            await expect(row).to_be_visible()
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            if screenshots:
                screenshots.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=screenshots / "automation-mobile.png", full_page=True)

        # Advance only the persisted deadline in this offline, temporary workspace.
        # The next run must be started by the real scheduler after reopening.
        store = Store(data_dir / "workspace.sqlite3")

        def make_due(state):
            next(r for r in state["rules"] if r["id"] == rule_id)["nextRunAt"] = (
                "2000-01-01T00:00:00+00:00"
            )

        store.mutate(make_due)
        gallery.images["sage"] = png("#786f45")
        async with (
            serve(create_app(settings)) as panel,
            httpx.AsyncClient(base_url=panel, trust_env=False) as client,
        ):
            state, rule = await wait_for(client, rule_id, 3, "completed", 2)
            assert (rule["lastNewCount"], rule["lastQueuedCount"]) == (1, 1), rule
            new_per_run.append(rule["lastNewCount"])
            await match_originals(client, state, gallery.images.values())

            # A background run can pause a rule while its edit dialog is open.
            # Saving only a new name must preserve the newer authorization state.
            await page.goto(panel + "/#automation")
            await row.locator("[data-action=edit-rule]").click()
            await form.locator("[name=name]").fill("花园图片更新")
            gallery.locked = True
            (await client.post(path + "/run")).raise_for_status()
            _, rule = await wait_for(client, rule_id, 4, "needs-user", 2)
            assert rule["enabled"] is False and rule["nextRunAt"] is None, rule
            assert (rule["lastNewCount"], rule["lastQueuedCount"]) == (0, 0), rule
            new_per_run.append(rule["lastNewCount"])
            renamed = await click_request(page, form.locator("[type=submit]"), "PATCH", path)
            rule = await renamed.json()
            assert (
                rule["name"] == "花园图片更新"
                and rule["enabled"] is False
                and rule["status"] == "needs-user"
            ), rule

        async with (
            serve(create_app(settings)) as panel,
            httpx.AsyncClient(base_url=panel, trust_env=False) as client,
        ):
            _, rule = await wait_for(client, rule_id, 4, "needs-user", 2)
            assert rule["enabled"] is False and rule["nextRunAt"] is None
            await page.goto(panel + "/#automation")
            await expect(row).to_be_visible()
            if screenshots:
                await page.set_viewport_size({"width": 1440, "height": 920})
                await page.screenshot(
                    path=screenshots / "automation-paused-desktop.png", full_page=True
                )

            gallery.locked = False
            await click_request(page, row.locator("[data-action=toggle-rule]"), "PATCH", path)
            await click_request(page, run, "POST", path + "/run")
            state, rule = await wait_for(client, rule_id, 5, "completed", 2)
            assert rule["enabled"] and (rule["lastNewCount"], rule["lastQueuedCount"]) == (0, 0)
            new_per_run.append(rule["lastNewCount"])
            await row.locator("[data-action=delete-rule]").click()
            await click_request(
                page, page.locator("[data-action=confirm-delete-rule]"), "DELETE", path
            )
            state = (await client.get("/api/state")).raise_for_status().json()
            assert not state["rules"] and len(state["jobs"]) == 2 and len(state["scans"]) == 5
            await match_originals(client, state, gallery.images.values())
        await browser.close()
    assert not errors, errors
    assert gallery.read_requests >= 4, "实际浏览器未访问公开画廊的读探针"
    assert gallery.write_requests == 0, f"自动化触发了 {gallery.write_requests} 次源站写请求"
    return {
        "runs": len(new_per_run),
        "newPerRun": new_per_run,
        "downloadedOriginalsVerified": len(state["assets"]),
        "timerAfterRestart": True,
        "authPauseAfterRestart": True,
        "mobileWidth": 390,
        "ruleDeletedViaPanel": True,
        "sourceReadProbes": gallery.read_requests,
        "sourceWriteRequests": gallery.write_requests,
        "frontendErrors": errors,
        "videoCheck": await verify_video_selection(data_dir / "video-case"),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="framekeep-automation-") as temporary:
        print(
            json.dumps(
                asyncio.run(verify(Path(temporary), args.screenshots)), ensure_ascii=False, indent=2
            )
        )

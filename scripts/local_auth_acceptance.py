"""Check desktop authorization using a real visible browser and an isolated local source.

Run: uv run python scripts/local_auth_acceptance.py
Requires a supported desktop, Chrome/Edge and lsof.
"""

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.local_auth import LocalAuthService  # noqa: E402
from backend.network import NetworkPolicy  # noqa: E402
from scripts.automation_acceptance import serve  # noqa: E402


async def check_window():
    source = FastAPI()
    ready = asyncio.Event()

    @source.get("/", response_class=HTMLResponse)
    async def login():
        return """<title>拾帧本机授权测试</title><p>合成授权样本</p><script>
        document.cookie='example=orchard; Path=/'; localStorage.setItem('plot','north');
        const db=indexedDB.open('plants',1); db.onupgradeneeded=()=>db.result.createObjectStore('notes');
        db.onsuccess=()=>{
          const tx=db.result.transaction('notes','readwrite');
          tx.objectStore('notes').put('fern','sample'); tx.oncomplete=()=>fetch('/ready');
        };</script>"""

    @source.get("/ready")
    async def mark_ready():
        ready.set()
        return {}

    async with serve(source) as origin:
        service = LocalAuthService(NetworkPolicy(allow_private_hosts={"127.0.0.1"}))
        site = {"id": "acceptance", "url": origin}
        try:
            assert (await service.open(site))["active"]
            await asyncio.wait_for(ready.wait(), 30)
            window = service._window
            assert window.sequence == 0
            # The verification proxy must reject even a loopback request from this process.
            reader, writer = await asyncio.open_connection("127.0.0.1", window.proxy.port)
            try:
                writer.write(f"GET {origin}/secret HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
                await writer.drain()
                try:
                    assert not await asyncio.wait_for(reader.read(256), 6)
                except ConnectionResetError:
                    pass
            finally:
                writer.close()
                with contextlib.suppress(ConnectionResetError):
                    await writer.wait_closed()

            async def fail(_site, _state):
                raise OSError("simulated storage failure")

            try:
                await service.save(site, fail)
            except OSError:
                pass
            else:
                raise AssertionError("模拟保存失败没有返回错误")
            assert service.info(site["id"])["active"]

            async def install(_site, state):
                assert any(
                    c["name"] == "example" and c["value"] == "orchard" for c in state["cookies"]
                )
                (data,) = state["origins"]
                assert data["origin"] == origin
                assert {"name": "plot", "value": "north"} in data["localStorage"]
                assert data["indexedDB"][0]["name"] == "plants"

            assert (await service.save(site, install))["saved"]
            assert not Path(window.profile).exists() and not service.info(site["id"])["active"]
        finally:
            await service.close()
    return {
        "visibleChromeLoadedFixture": True,
        "verificationCDPCommands": 0,
        "unrelatedProxyClientRejected": True,
        "snapshotAfterRetry": ["cookie", "localStorage", "IndexedDB"],
        "profileRemovedOnSave": True,
    }


async def check_system_window():
    from playwright.async_api import async_playwright

    source = FastAPI()
    ready = asyncio.Event()
    home_requests = []
    direct_probe_requests = []
    browser_facts = []

    @source.get("/", response_class=HTMLResponse)
    async def login():
        home_requests.append(True)
        return """<title>拾帧系统浏览器授权测试</title><script>
        (async () => {
          if (!localStorage.getItem('plot')) {
            document.cookie='session=fern; Path=/';
            document.cookie='persistent=orchard; Max-Age=3600; Path=/';
            localStorage.setItem('plot','north');
            const db=await new Promise(r=>{
              const opened=indexedDB.open('plants',1);
              opened.onupgradeneeded=()=>opened.result.createObjectStore('notes');
              opened.onsuccess=()=>r(opened.result);
            });
            const tx=db.transaction('notes','readwrite');
            tx.objectStore('notes').put('fern','sample');
            await new Promise(r=>tx.oncomplete=r);
            db.close();
          }
          await fetch('/ready?webdriver='+navigator.webdriver);
        })();</script>"""

    @source.get("/ready")
    async def mark_ready(webdriver: str):
        browser_facts.append(webdriver)
        ready.set()
        return {}

    @source.get("/.framekeep-session-export")
    async def unexpected_probe():
        direct_probe_requests.append(True)
        return {}

    async with serve(source) as origin:
        service = LocalAuthService(NetworkPolicy(allow_private_hosts={"127.0.0.1"}))
        site = {"id": "system-fixture", "url": origin}
        try:
            with patch("backend.local_auth._system_origin", return_value=True):
                assert (await service.open(site))["mode"] == "system-browser"
            await asyncio.wait_for(ready.wait(), 30)
            window = service._window
            root = window.root
            pid = window.pid
            args = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True)
            assert all(
                flag not in args
                for flag in ("--remote-debugging", "--enable-automation", "--proxy-")
            )
            assert browser_facts == ["false"]
            ready.clear()

            async def fail(_site, state):
                assert len(home_requests) == 1
                assert {c["name"] for c in state["cookies"]} == {"persistent", "session"}
                raise OSError("simulated storage failure")

            try:
                await service.save(site, fail)
            except OSError:
                pass
            else:
                raise AssertionError("模拟保存失败没有返回错误")
            await asyncio.wait_for(ready.wait(), 20)
            assert window.pid != pid and root.exists()
            count_before_export = len(home_requests)
            imported = {}

            async def install(_site, state):
                assert len(home_requests) == count_before_export
                assert {c["name"]: c["value"] for c in state["cookies"]} == {
                    "persistent": "orchard",
                    "session": "fern",
                }
                (data,) = state["origins"]
                assert {"name": "plot", "value": "north"} in data["localStorage"]
                records = data["indexedDB"][0]["stores"][0]["records"]
                assert records == [{"key": "sample", "value": "fern"}]
                imported.update(state)

            assert (await service.save(site, install))["saved"]
            assert not root.exists() and not direct_probe_requests
            # Exercise the same public Playwright storage_state format used by scanning.
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                context = await browser.new_context(storage_state=imported, offline=True)
                page = await context.new_page()
                await page.route(
                    origin + "/check",
                    lambda r: r.fulfill(content_type="text/html", body="<title>restored</title>"),
                )
                await page.goto(origin + "/check")
                assert await page.evaluate("localStorage.getItem('plot')") == "north"
                assert await page.evaluate("document.cookie.split('; ').sort()") == [
                    "persistent=orchard",
                    "session=fern",
                ]
                assert (
                    await page.evaluate("""async () => {
                  const db = await new Promise(r=>{const q=indexedDB.open('plants'); q.onsuccess=()=>r(q.result)});
                  return await new Promise(r=>{const q=db.transaction('notes').objectStore('notes').get('sample'); q.onsuccess=()=>r(q.result)});
                }""")
                    == "fern"
                )
                await browser.close()
            return {
                "webdriver": False,
                "debugOrProxyFlags": False,
                "offlineSourceProbeRequests": 0,
                "preservedOnWriteFailure": True,
                "retryRestoredSession": [
                    "sessionCookie",
                    "persistentCookie",
                    "localStorage",
                    "IndexedDB",
                ],
                "profileRemovedOnSave": True,
            }
        finally:
            await service.close()


async def orphan_fixture(origin, system):
    service = LocalAuthService(NetworkPolicy(allow_private_hosts={"127.0.0.1"}))
    with patch("backend.local_auth._system_origin", return_value=system):
        await service.open({"id": "orphan-check", "url": origin})
    window = service._window
    print(
        json.dumps(
            [
                str(window.root if system else window.profile),
                window.pid if system else window.proxy.group,
            ]
        ),
        flush=True,
    )
    await asyncio.Event().wait()


async def check_parent_exit(system=False):
    app = FastAPI()

    @app.get("/")
    async def root():
        return {"fixture": "orphan cleanup"}

    async with serve(app) as origin:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            __file__,
            "--system-child" if system else "--child",
            origin,
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            record = await asyncio.wait_for(child.stdout.readline(), 15)
            profile, chrome_pid = json.loads(record)
            assert Path(profile).is_dir()
            os.kill(child.pid, signal.SIGKILL)
            await child.wait()
            async with asyncio.timeout(15):
                while Path(profile).exists():
                    await asyncio.sleep(0.1)
            try:
                os.kill(chrome_pid, 0)
            except ProcessLookupError:
                return True
            raise AssertionError("服务意外退出后专用浏览器仍在运行")
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()


async def main():
    if len(sys.argv) == 3 and sys.argv[1] in {"--child", "--system-child"}:
        await orphan_fixture(sys.argv[2], sys.argv[1] == "--system-child")
    else:
        report = await check_window()
        report["browserAndProfileRemovedOnParentCrash"] = await check_parent_exit()
        if sys.platform == "darwin":
            report["systemBrowser"] = await check_system_window()
            report["systemBrowser"][
                "browserAndProfileRemovedOnParentCrash"
            ] = await check_parent_exit(system=True)
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

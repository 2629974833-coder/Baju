"""Persistent browser windows are capped without discarding saved login state."""

import asyncio

import pytest

from backend.browser import BrowserService, _Session


class Page:
    def __init__(self, context):
        self.context = context
        self.closed = False
        self.url = "about:blank"
        self.main_frame = object()

    def is_closed(self):
        return self.closed

    def on(self, *_):
        pass

    async def goto(self, url, **_):
        self.url = url
        await self.context.pause("navigate")

    async def title(self):
        return "Fixture"


class Context:
    def __init__(self, browser):
        self.browser = browser
        self.pages = []
        self.closed = False

    async def pause(self, stage):
        if self.browser.block_stage == stage:
            self.browser.entered.set()
            await self.browser.release.wait()

    def set_default_timeout(self, _):
        pass

    def set_default_navigation_timeout(self, _):
        pass

    async def route(self, *_):
        await self.pause("route")

    async def route_web_socket(self, *_):
        pass

    def on(self, *_):
        pass

    async def new_page(self):
        page = Page(self)
        self.pages.append(page)
        return page

    async def close(self):
        self.closed = True
        for page in self.pages:
            page.closed = True


class Browser:
    def __init__(self):
        self.contexts = []
        self.block_stage = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def new_context(self, **_):
        # Two sites can reach allocation together unless admission is serialized.
        await asyncio.sleep(0)
        context = Context(self)
        self.contexts.append(context)
        return context


class Policy:
    async def validate(self, url):
        await asyncio.sleep(0)
        return url


class Service(BrowserService):
    async def start(self):
        pass

    def _saved_state(self, _):
        return None


def service_at(path, monkeypatch, limit):
    async def document(_self, _frame):
        pass

    monkeypatch.setattr(_Session, "ensure_document", document)
    service = Service(path, Policy(), max_open_sessions=limit)
    service._browser = Browser()
    return service


def site(identifier):
    return {"id": identifier, "url": f"https://{identifier}.example/"}


@pytest.mark.asyncio
async def test_different_sites_share_persistent_window_limit(tmp_path, monkeypatch):
    service = service_at(tmp_path, monkeypatch, 2)
    results = await asyncio.gather(*(service.open_session(site(name)) for name in ("first", "second", "third")),
                                   return_exceptions=True)
    assert sum(isinstance(result, dict) and result["open"] for result in results) == 2
    rejected = [result for result in results if isinstance(result, ValueError)]
    assert len(rejected) == 1 and "来源" in str(rejected[0]) and "断开" in str(rejected[0])
    assert len(service._browser.contexts) == 2
    assert len(service._sessions) == 2


@pytest.mark.asyncio
async def test_existing_site_reuses_its_window_at_capacity(tmp_path, monkeypatch):
    service = service_at(tmp_path, monkeypatch, 1)
    first = await service.open_session(site("first"))
    reused = await service.open_session(site("first"))
    assert reused["activeTabId"] == first["activeTabId"]
    assert len(service._browser.contexts) == 1


@pytest.mark.asyncio
async def test_closed_window_releases_capacity_without_deleting_saved_login(tmp_path, monkeypatch):
    service = service_at(tmp_path, monkeypatch, 1)
    await service.open_session(site("first"))
    saved = service._state_path("first")
    saved.parent.mkdir()
    saved.write_bytes(b"saved-session-fixture")
    old = service._browser.contexts[0]
    old.pages[0].closed = True
    assert (await service.open_session(site("second")))["open"]
    assert old.closed and set(service._sessions) == {"second"}
    assert saved.read_bytes() == b"saved-session-fixture"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["route", "navigate"])
async def test_cancelled_window_creation_releases_context_and_capacity(tmp_path, monkeypatch, stage):
    service = service_at(tmp_path, monkeypatch, 1)
    browser = service._browser
    browser.block_stage = stage
    creating = asyncio.create_task(service.open_session(site("first")))
    await asyncio.wait_for(browser.entered.wait(), 1)
    creating.cancel()
    with pytest.raises(asyncio.CancelledError):
        await creating
    assert not service._sessions and browser.contexts[0].closed
    browser.block_stage = None
    assert (await asyncio.wait_for(service.open_session(site("second")), 1))["open"]


@pytest.mark.asyncio
async def test_local_mode_keeps_unlimited_windows(tmp_path, monkeypatch):
    service = service_at(tmp_path, monkeypatch, None)
    results = await asyncio.gather(*(service.open_session(site(str(index))) for index in range(4)))
    assert all(result["open"] for result in results)
    assert len(service._sessions) == 4

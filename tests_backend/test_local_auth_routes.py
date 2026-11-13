from fastapi.testclient import TestClient

from tests_backend.test_app import app_at


def test_native_auth_requires_both_local_peer_and_local_panel_origin(tmp_path):
    app = app_at(tmp_path)
    api = "/api/sites/chunlou/local-auth"
    with TestClient(app, base_url="http://localhost", client=("198.51.100.5", 1234)) as client:
        info = client.get(api).json()
        assert not info["available"] and not info["active"]
        assert client.post(api, json={}).status_code == 403
        assert client.post(api + "/save", json={}).status_code == 403
        assert client.delete(api).status_code == 403
    with TestClient(app_at(tmp_path), base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as client:
        assert not client.get(api, headers={"X-Forwarded-For": "127.0.0.1"}).json()["available"]
        assert client.post(api, headers={"Origin": "https://outside.test"}, json={}).status_code == 403


def test_save_only_marks_site_after_successful_native_import(tmp_path, monkeypatch):
    app = app_at(tmp_path)
    api = "/api/sites/chunlou/local-auth"
    seen = []
    snapshot = {"cookies": [], "origins": []}

    async def save(site, install):
        seen.append(site["id"])
        return await install(site, snapshot)

    async def reject(_site, _snapshot):
        raise ValueError("导入失败")

    async def succeed(site, value):
        assert site["id"] == "chunlou" and value == snapshot
        return {"open": False, "saved": True, "verified": False}

    monkeypatch.setattr(app.state.local_auth, "save", save)
    monkeypatch.setattr(app.state.browser, "import_local_session", reject, raising=False)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as c:
        def saved():
            return next(site for site in c.get("/api/state").json()["sites"] if site["id"] == "chunlou")
        assert c.post(api + "/save", json={}).status_code == 400
        assert not saved()["sessionSaved"]
        monkeypatch.setattr(app.state.browser, "import_local_session", succeed)
        assert c.post(api + "/save", json={}).json() == {"open": False, "saved": True, "verified": False}
        assert saved()["sessionSaved"] and not saved()["verified"]
        app.state.store.mutate(lambda s: s["scans"].append(
            {"id": "ongoing", "siteId": "chunlou", "status": "running", "url": "https://chunlou.tv/"}))
        assert c.post(api + "/save", json={}).status_code == 409
        assert seen == ["chunlou", "chunlou"]


def test_www_chunlou_link_reuses_canonical_saved_site_and_scan_url(tmp_path):
    app = app_at(tmp_path)
    episode = "/feed?ep=a1111111-aaaa-4222-a333-000000000001"
    bare = "https://chunlou.tv" + episode
    alias = "https://www.chunlou.tv:443" + episode
    with TestClient(app) as client:
        for scope in ("single", "series"):
            result = client.post("/api/intake", json={"url": alias, "scope": scope})
            assert result.status_code == 200, result.text
            assert (result.json()["siteId"], result.json()["url"]) == ("chunlou", bare)
        # Pre-existing workspaces can have a separate saved account under www.
        def add_legacy(state):
            original = next(s for s in state["sites"] if s["id"] == "chunlou")
            state["sites"].append({
                **original, "id": "legacy-www", "url": "https://www.chunlou.tv/", "sessionSaved": True,
            })

        app.state.store.mutate(add_legacy)
        for scope in ("single", "series"):
            for selected in ({}, {"siteId": "legacy-www"}):
                result = client.post("/api/intake", json={"url": bare, **selected, "scope": scope})
                assert result.status_code == 200, result.text
                assert (result.json()["siteId"], result.json()["url"]) == ("legacy-www", bare)


def test_lata_alias_reuses_source_and_native_auth_opens_google_login_page(tmp_path, monkeypatch):
    app = app_at(tmp_path)
    opened = []

    async def open_local(site, url):
        opened.append((site['id'], url))
        return {'available': True, 'active': True, 'mode': 'system-browser'}

    monkeypatch.setattr(app.state.local_auth, 'open', open_local)
    with TestClient(app, base_url='http://127.0.0.1:4173', client=('127.0.0.1', 41000)) as client:
        source = client.post('/api/sites', json={'name':'Lata', 'url':'https://www.lata.casa:443/series'}).raise_for_status().json()
        assert source['url'] == 'https://lata.casa/series'
        response = client.post('/api/sites/'+source['id']+'/local-auth')
        assert response.status_code == 200 and response.json()['mode']=='system-browser'
        assert opened == [(source['id'], 'https://lata.casa/login')]
        duplicate = client.post('/api/sites', json={'name':'Lata', 'url':'https://lata.casa/series'}).raise_for_status().json()
        assert duplicate['id'] == source['id']

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.browser import BrowserService
from tests_backend.test_app import app_at, until


def test_original_preflight_and_download_return_the_same_named_file(tmp_path):
    with TestClient(app_at(tmp_path)) as client:
        client.post('/api/scans', json={
            'siteId': 'chunlou', 'url': 'https://chunlou.tv', 'depth': 1,
            'limit': 3, 'kinds': ['image'],
        })
        state = until(client, lambda s: s['scans'][0]['status'] == 'completed')
        asset = state['assets'][0]
        client.post('/api/downloads', json={'ids': [asset['id']]})
        until(client, lambda s: s['jobs'][0]['status'] == 'completed')
        url = f"/api/assets/{asset['id']}/file?download=true"
        head, download = client.head(url), client.get(url)
        assert head.status_code == download.status_code == 200
        assert head.content == b''
        assert download.content == b'verified-file-fixture'
        assert 'attachment;' in head.headers['content-disposition']
        assert head.headers['content-type'] == 'image/png'
        assert head.headers['content-length'] == str(len(download.content))
        assert client.get(url, headers={'range':'bytes=0-7'}).content == b'verified'
        assert client.head('/api/assets/missing/file?download=true').status_code == 404


@pytest.mark.asyncio
async def test_drag_releases_mouse_even_on_failure_and_rejects_invalid_paths(tmp_path):
    service = BrowserService(tmp_path, None)
    mouse = SimpleNamespace(move=AsyncMock(), down=AsyncMock(), up=AsyncMock())
    page = SimpleNamespace(mouse=mouse)
    service._sessions['test'] = SimpleNamespace(current=lambda: page)
    service.session_info = AsyncMock(return_value={'open':True})
    points = [{'x':20,'y':30,'t':0}, {'x':200,'y':30,'t':16}]
    await service.interact('test', {'type':'drag','points':points})
    mouse.down.assert_awaited_once()
    mouse.up.assert_awaited_once()
    mouse.move.reset_mock()
    mouse.up.reset_mock()
    mouse.move.side_effect = [None, RuntimeError('closed')]
    with pytest.raises(RuntimeError):
        await service.interact('test', {'type':'drag','points':points})
    mouse.up.assert_awaited_once()
    for bad in [[], points * 130, [points[0], {'x':2000,'y':30,'t':16}],
                [points[0], {'x':20,'y':30,'t':float('nan')}]]:
        with pytest.raises(ValueError):
            await service.interact('test', {'type':'drag','points':bad})

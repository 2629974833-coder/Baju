import asyncio
import io
import json
import zipfile

from fastapi.testclient import TestClient

from backend.series import series_identifier, series_library
from tests_backend.test_app import app_at, until
from tests_backend.test_series_library import catalog, scan


def site_scan(cat):
    return {'id': 'site-scan', 'siteId': 'example', 'url': 'https://example.com/',
            'mode': 'site', 'intakeScope': 'site', 'status': 'completed',
            'createdAt': '2026-11-11', 'siteCatalog': {'shows': [
                {'url': cat['catalogUrl'], 'title': cat['title'], 'catalog': cat,
                 'categories': ['自然', '课程'], '_indexCovers': ['https://example.com/poster.png']}
            ]}}


def test_site_catalog_joins_single_show_identity_and_preserves_categories():
    cat = catalog()
    cat['coverUrls'] = ['https://example.com/poster.png']
    state = {'scans': [scan('single', catalog((1,))), site_scan(cat)], 'assets': []}
    rows = series_library(state)
    assert len(rows) == 1
    assert len(rows[0]['catalog']['entries']) == 3
    assert rows[0]['categories'] == ['自然', '课程']
    assert rows[0]['sourceScanIds'] == ['site-scan', 'single']
    assert rows[0]['artwork'] == {'total': 1, 'saved': 0, 'pending': 1, 'missing': 0, 'assetIds': []}
    state['_dismissedSeries'] = [rows[0]['id']]
    assert not series_library(state)


class EpisodeScanner:
    def __init__(self, *, hold=False):
        self.seen = []
        self.hold = hold

    async def scan(self, site, options, on_asset, on_progress, cancelled):
        self.seen.append(options)
        if self.hold:
            await asyncio.Event().wait()
        cat = options['catalog']
        entry = next(e for e in cat['entries'] if e['pageUrl'] == options['url'])
        if entry['ordinal'] == 3:
            result = {'status': 'needs-user', 'pagesVisited': 1, 'catalog': cat,
                      '_pageAccess': {entry['pageUrl']: 'needs-user'}}
            await on_progress(result)
            return result
        await on_asset({'sourceUrl': f"https://example.com/media/episode-{entry['ordinal']}.mp4",
                        'pageUrl': entry['pageUrl'], 'kind': 'video', 'engine': 'http',
                        'title': 'Episode', '_readOnly': True})
        result = {'status': 'completed', 'pagesVisited': 1, 'catalog': cat,
                  '_pageAccess': {entry['pageUrl']: 'observed'}}
        await on_progress(result)
        return result


def seed(app):
    cat = catalog()
    cat['coverUrls'] = ['https://example.com/poster.png']
    cat['entries'][2].update(access='needs-user', accessEvidence='player')
    def write(state):
        state['sites'].append({'id': 'example', 'url': 'https://example.com/', 'name': '来源'})
        state['scans'] = [site_scan(cat)]
    app.state.store.mutate(write)
    return series_identifier({'siteId': 'example', 'url': cat['catalogUrl'], 'catalog': cat})


def test_whole_show_download_resolves_then_queues_videos_and_real_poster(tmp_path):
    app = app_at(tmp_path)
    scanner = EpisodeScanner()
    app.state.tasks.scanner = scanner
    identifier = seed(app)
    with TestClient(app) as client:
        response = client.post(f'/api/series/{identifier}/downloads', json={'resolveMissing': True})
        assert response.status_code == 200, response.text
        assert response.json()['resolving'] and response.json()['scanId']
        state = until(client, lambda s: len(s['jobs']) == 3 and all(j['status'] == 'completed' for j in s['jobs']))
        assert len(scanner.seen) == 3
        assert all(o.get('readOnly') and 'autoUnlock' not in o for o in scanner.seen)
        row = state['series'][0]
        assert row['catalog']['downloadedCount'] == 2
        assert row['artwork']['saved'] == 1 and len(row['artwork']['assetIds']) == 1
        report = state['scans'][0]['downloadSummary']
        assert report['added'] == 3 and len(report['unavailable']) == 1
        archive = client.get(f'/api/series/{identifier}/archive')
        assert archive.status_code == 200
        with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
            names = z.namelist()
            assert all(n.startswith('花园观察/') for n in names)
            assert '花园观察/海报.png' in names
            assert z.read('花园观察/海报.png') == b'verified-file-fixture'
            manifest = json.loads(z.read('花园观察/manifest.json'))
            assert manifest['savedCount'] == 2 and manifest['savedArtworkCount'] == 1
            assert not manifest['allSaved']
        key = row['catalog']['entries'][0]['episodeKey']
        single = client.get(f'/api/series/{identifier}/episode-file', params={'episodeKey': key})
        assert single.status_code == 200 and 'E001' in single.headers['content-disposition']
        repeated = client.post(f'/api/series/{identifier}/downloads', json={'resolveMissing': True}).json()
        assert repeated['resolving']
        state = until(client, lambda s: 'downloadSummary' in s['scans'][0])
        assert state['scans'][0]['downloadSummary']['duplicates'] == 3
        assert len(state['jobs']) == 3
    with TestClient(app_at(tmp_path)) as client:
        state = client.get('/api/state').json()
        assert len(state['jobs']) == 3 and state['series'][0]['artwork']['saved'] == 1


def test_cancelled_prepare_never_starts_downloads(tmp_path):
    app = app_at(tmp_path)
    app.state.tasks.scanner = EpisodeScanner(hold=True)
    identifier = seed(app)
    with TestClient(app) as client:
        response = client.post(f'/api/series/{identifier}/downloads', json={'resolveMissing': True})
        assert response.status_code == 200
        scan_id = response.json()['scanId']
        until(client, lambda s: s['scans'][0]['status'] == 'running')
        assert client.post(f'/api/scans/{scan_id}/cancel').status_code == 200
        until(client, lambda s: s['scans'][0]['status'] == 'cancelled')
        assert client.get('/api/state').json()['jobs'] == []
    with TestClient(app_at(tmp_path)) as client:
        assert client.get('/api/state').json()['jobs'] == []


def test_site_projection_rejects_cross_origin_show():
    cat = catalog(origin='https://elsewhere.example')
    assert series_library({'scans': [site_scan(cat)], 'assets': []}) == []


def test_archive_reports_missing_artwork_without_fake_files(tmp_path):
    from backend.archive import prepare_series_archive
    cat = catalog()
    cat['coverUrls'] = ['https://example.com/poster.png']
    plan = prepare_series_archive(cat, [], {'id': 'example'}, tmp_path)
    assert not plan['files']
    assert plan['manifest']['savedArtworkCount'] == 0
    assert plan['manifest']['artwork'][0]['state'] == 'not-downloaded'


def test_index_only_show_can_resolve_download_and_keep_its_category(tmp_path, monkeypatch):
    from tests_backend.test_lata_scanning import setup_lata
    app, _, _ = setup_lata(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.post('/api/intake', json={'url': 'https://lata.casa/series', 'scope': 'site',
                                                   'limit': 1, 'catalogOnly': True})
        assert response.status_code == 200, response.text
        state = until(client, lambda s: s['scans'][0]['status'] == 'completed')
        pending = state['series'][0]
        assert pending['directoryPending'] and not pending['catalog']['entries']
        assert state['scans'][0]['siteSummary'] == {'shows': 1, 'episodes': 0, 'directories': 0}
        assert state['scans'][0]['canContinue']
        result = client.post(f"/api/series/{pending['id']}/downloads", json={'resolveMissing': True})
        assert result.status_code == 200, result.text
        state = until(client, lambda s: len(s['jobs']) == 2 and all(j['status'] == 'completed' for j in s['jobs']))
        assert len(state['series']) == 1 and not state['series'][0]['directoryPending']
        assert len(state['series'][0]['catalog']['entries']) == 3
        assert state['series'][0]['artwork']['saved'] == 1


def test_catalog_only_continuation_reads_directories_without_scanning_players(tmp_path, monkeypatch):
    from tests_backend.test_lata_scanning import setup_lata
    app, _, seen = setup_lata(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.post('/api/intake', json={'url': 'https://lata.casa/series', 'scope': 'site',
                                                   'limit': 1, 'catalogOnly': True})
        first = response.json()['id']
        until(client, lambda s: s['scans'][0]['status'] == 'completed')
        response = client.post('/api/intake/'+first+'/continue', json={'limit': 10})
        assert response.status_code == 200
        state = until(client, lambda s: s['scans'][0]['status'] == 'completed')
        assert state['scans'][0]['catalogOnly'] and not state['scans'][0]['canContinue']
        assert seen == ['https://lata.casa/originals/garden']
        assert not any(a['kind'] == 'video' for a in state['assets'])
        assert len(state['series']) == 1


def test_old_site_catalog_cannot_restore_removal_but_fresh_scan_can(tmp_path):
    app = app_at(tmp_path)
    identifier = seed(app)
    class IndexScanner:
        async def scan(self, site, options, on_asset, on_progress, cancelled):
            await on_progress({'status': 'completed', 'pagesVisited': 1,
                               'siteCatalog': site_scan(catalog())['siteCatalog']})
    app.state.tasks.scanner = IndexScanner()
    with TestClient(app) as client:
        assert client.post('/api/series/dismiss', json={'ids':[identifier]}).status_code == 200
        assert not client.get('/api/state').json()['series']
        response = client.post('/api/intake', json={'url':'https://example.com/', 'siteId':'example',
                                                   'scope':'site', 'catalogOnly': True})
        assert response.status_code == 200
        state = until(client, lambda s: s['scans'][0]['status'] == 'completed')
        assert len(state['series']) == 1 and state['series'][0]['id'] == identifier


def test_completed_download_plan_recovery_is_atomic_and_deduplicated(tmp_path):
    app = app_at(tmp_path)
    identifier = seed(app)
    def write(state):
        source = catalog()
        state['assets'] = [{'id':'video', 'siteId':'example','kind':'video','engine':'http','title':'Episode',
                            'status':'discovered','pageUrl':source['entries'][0]['pageUrl'],
                            'sourceUrl':'https://example.com/media/episode-1.mp4'}]
        state['scans'].insert(0, scan('finished-before-queue', source,
            downloadAfterScan=True,
            _seriesDownload={'seriesId':identifier,'episodeKeys':[source['entries'][0]['episodeKey']],
                             'options':{},'covers':['https://example.com/poster.png']}))
    app.state.store.mutate(write)
    with TestClient(app) as client:
        state = until(client, lambda s: len(s['jobs']) == 2 and all(j['status']=='completed' for j in s['jobs']))
        assert state['scans'][0]['downloadSummary']['added'] == 2
    with TestClient(app_at(tmp_path)) as client:
        assert len(client.get('/api/state').json()['jobs']) == 2


def test_hidden_index_card_is_not_revived_by_inflight_directory_result():
    index = site_scan(catalog())
    index['siteCatalog']['shows'][0]['catalog'] = {}
    state = {'scans': [index], 'assets': []}
    original = series_library(state)[0]
    state['_seriesTitles'] = {original['id']: '自定剧名'}
    state['scans'].insert(0, scan('direct', catalog()))
    rows = series_library(state)
    assert len(rows) == 1 and rows[0]['title'] == '自定剧名'
    state['_dismissedSeries'] = [original['id']]
    assert series_library(state) == []


def test_completed_directory_phase_is_not_mistaken_for_finished_episode_checks(tmp_path):
    app = app_at(tmp_path)
    identifier = seed(app)
    def write(state):
        state['scans'].insert(0, scan('directory-before-check', catalog(),
            downloadAfterScan=True,
            _seriesDownload={'seriesId': identifier, 'phase': 'directory', 'episodeKeys': None,
                             'options': {}, 'covers': ['https://example.com/poster.png']}))
    app.state.store.mutate(write)
    with TestClient(app) as client:
        state = client.get('/api/state').json()
        assert not state['jobs']
        assert state['scans'][0]['status'] == 'failed'
        assert 'downloadSummary' not in state['scans'][0]


def test_removed_index_card_returns_after_explicit_single_show_rescan(tmp_path, monkeypatch):
    from tests_backend.test_lata_scanning import SHOW, setup_lata
    app, _, _ = setup_lata(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.post('/api/intake', json={'url': 'https://lata.casa/series', 'scope': 'site',
                                        'limit': 1, 'catalogOnly': True})
        state = until(client, lambda s: s['scans'][0]['status'] == 'completed')
        pending_id = state['series'][0]['id']
        client.post('/api/series/dismiss', json={'ids': [pending_id]})
        response = client.post('/api/series', json={'url': SHOW})
        assert response.status_code == 200, response.text
        state = until(client, lambda s: s['scans'][0]['status'] not in {'running', 'queued'})
        assert len(state['series']) == 1
        assert state['series'][0]['catalog']['enumeratedCount'] == 3
        assert not state['dismissedSeriesCount']

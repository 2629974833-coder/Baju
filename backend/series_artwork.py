"""Publisher artwork associations shared by series views, queues and exports."""

import hashlib
from urllib.parse import urlsplit


def artwork_urls(row, assets):
    urls = list(row.get('catalog', {}).get('coverUrls') or [])
    for asset in assets:
        if asset.get('siteId') != row.get('siteId') or asset.get('kind') != 'image':
            continue
        if row.get('id') in asset.get('_seriesArtworkIds', []):
            urls.append(asset.get('sourceUrl'))
        elif row.get('catalog', {}).get('adapterId') == 'chunlou':
            prefix = f"https://hls.chunlou.tv/cover/{row['catalog']['seriesKey']}/"
            if (asset.get('sourceUrl') or '').startswith(prefix):
                urls.append(asset['sourceUrl'])
    result = []
    for url in urls:
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in {'https', 'http'} and parsed.hostname
                     and not parsed.username and not parsed.password and parsed.port != 0)
        except (TypeError, ValueError):
            valid = False
        if valid and url not in result:
            result.append(url)
    return result[:12]


def artwork_assets(row, assets):
    urls = set(artwork_urls(row, assets))
    return [a for a in assets if a.get('siteId') == row.get('siteId') and a.get('kind') == 'image'
            and a.get('sourceUrl') in urls]


def artwork_summary(row, assets):
    urls = artwork_urls(row, assets)
    images = artwork_assets(row, assets)
    saved = {a['sourceUrl'] for a in images if a.get('status') == 'downloaded' and a.get('_path')}
    return {'total': len(urls), 'saved': len(saved), 'pending': len(urls) - len(saved),
            'missing': int(not urls), 'assetIds': [a['id'] for a in images]}


def ensure_artwork_assets(state, row):
    from backend.store import timestamp

    ids = []
    for url in artwork_urls(row, state['assets']):
        asset = next((a for a in state['assets'] if a.get('siteId') == row['siteId']
                      and a.get('kind') == 'image' and a.get('sourceUrl') == url), None)
        if asset is None:
            if len(state['assets']) >= 10000:
                raise ValueError('素材库已达到本版本的 10000 条容量上限')
            identifier = 'asset_' + hashlib.sha256((row['siteId'] + '\0' + url).encode()).hexdigest()[:32]
            asset = {'id': identifier, 'siteId': row['siteId'], 'kind': 'image', 'engine': 'http',
                     'sourceUrl': url, 'pageUrl': row['url'], 'title': row['title'] + ' · 海报',
                     'category': '图片素材', 'date': timestamp()[:10], 'duration': '—', 'sizeMB': 0,
                     'status': 'discovered', 'locked': False, 'tags': [], 'favorite': False,
                     'versions': [], '_readOnly': True, '_headers': {'Referer': row['url']},
                     '_thumbnailUrl': url, 'thumbnail': f'/api/assets/{identifier}/thumbnail'}
            state['assets'].append(asset)
        owners = asset.setdefault('_seriesArtworkIds', [])
        if row['id'] not in owners:
            owners.append(row['id'])
        ids.append(asset['id'])
    return ids

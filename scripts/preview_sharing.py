"""Loopback-only preview with an explicitly supplied sample video; never uses live accounts."""

import argparse
import hashlib
import shutil
from pathlib import Path

import uvicorn
from fastapi.responses import HTMLResponse

from backend.app import create_app
from backend.config import Settings
from backend.store import timestamp


def preview_app(sample, port=4177):
    root = Path(__file__).resolve().parents[1] / '.preview-data'
    target = root / 'assets/preview_video/original.mp4'
    target.parent.mkdir(parents=True, exist_ok=True)
    if Path(sample).resolve() != target.resolve():
        shutil.copyfile(sample, target)
    settings = Settings(data_dir=root, allow_private_hosts={'127.0.0.1'})
    app = create_app(settings)
    now = timestamp()
    asset = {
        'id':'preview_video', 'siteId':'preview', 'kind':'video', 'status':'downloaded',
        'title':'山间慢镜头 · 本地示例视频', 'category':'本地示例', 'tags':['预览'],
        'pageUrl':f'http://127.0.0.1:{port}/preview-slider',
        'sourceUrl':f'http://127.0.0.1:{port}/api/assets/preview_video/file',
        '_path':str(target.relative_to(root)), 'mime':'video/mp4',
        'sizeBytes':target.stat().st_size, 'sizeMB':target.stat().st_size / 1024**2,
        'width':720, 'height':480, 'duration':'0:05', 'date':now[:10],
        'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
        'thumbnail':'/assets/mountain.jpg', 'fileUrl':'/api/assets/preview_video/file',
        'versions':[], 'favorite':False, 'locked':False,
    }

    def seed(state):
        state['assets'] = [asset]
        state['jobs'] = [{'id':'preview_job','assetId':asset['id'],'type':'download',
                          'status':'completed','progress':100,'createdAt':now,'completedAt':now}]
        state['scans'] = []
        state['sites'] = [{'id':'preview','name':'本地拖动测试','domain':'127.0.0.1',
                           'url':asset['pageUrl'],'initials':'测','auth':'disconnected',
                           'sessionSaved':False,'verified':False,'count':1,'access':'仅用于预览'}]
    app.state.store.mutate(seed)
    static = app.router.routes.pop()

    @app.get('/preview-slider')
    async def slider():
        return HTMLResponse('''<!doctype html><meta charset="utf-8"><title>人工拖动测试</title>
        <style>body{margin:0;background:#e5ece7;font:20px sans-serif;display:grid;place-items:center;height:100vh}
        section{padding:40px;background:white;border-radius:20px;width:460px}input{width:100%;height:60px;accent-color:#23875d}p{color:#516257}</style>
        <section><h1>拖动操作测试</h1><p>用鼠标或手指，把滑块拖到右边。</p>
        <input aria-label="测试滑块" type="range" min="0" max="100" value="0"
        oninput="document.querySelector('output').textContent=this.value>=90?'拖动已成功传到站点页面':'当前位置：'+this.value">
        <output>当前位置：0</output><p>这是本机测试页面，不是真实验证码。</p></section>''')

    app.router.routes.append(static)
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sample', required=True)
    parser.add_argument('--port', type=int, default=4177)
    args = parser.parse_args()
    uvicorn.run(preview_app(args.sample, args.port), host='127.0.0.1', port=args.port, access_log=False)

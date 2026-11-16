"""Exercise real button handlers against a GET-only service and a simulated phone port."""
import base64
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def test_get_only_video_share_save_and_visible_missing_file():
    root = Path(__file__).resolve().parents[1] / 'dist'
    video = bytes(range(251)) * 400

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, *_):
            pass

        def do_HEAD(self):
            self.send_error(404)

        def do_GET(self):
            if self.path.startswith('/api/assets/'):
                if '/missing/' in self.path:
                    self.send_error(404)
                    return
                ranged = self.headers.get('Range') == 'bytes=0-0'
                self.send_response(206 if ranged else 200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Length', '1' if ranged else str(len(video)))
                if ranged:
                    self.send_header('Content-Range', f'bytes 0-0/{len(video)}')
                self.end_headers()
                self.wfile.write(video[:1] if ranged else video)
                return
            if self.path == '/fixture':
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(b'<script type="module">import {bindMediaActions} from "/media-actions.js";'
                                 b'window.actions=bindMediaActions({canProcess:()=>false,onProcess:()=>{},toast:()=>{}});</script>')
                return
            super().do_GET()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={'width': 393, 'height': 750})
            page.goto(f'http://127.0.0.1:{server.server_port}/fixture')
            page.wait_for_function('window.actions')
            page.evaluate('''() => {
              const c = new MessageChannel(); window.calls=[]; window.bytes=[];
              c.port2.onmessage = ({data}) => {
                const m=JSON.parse(data); window.calls.push(m);
                if(m.op==='chunk')window.bytes.push(m.data);
                c.port2.postMessage(JSON.stringify({id:m.id,ok:true,message:'fixture-complete'}));
              };
              window.dispatchEvent(new MessageEvent('message', {data:'framekeep-native-v1', origin:'', source:null, ports:[c.port1]}));
            }''')
            for intent in ['share', 'save']:
                page.evaluate('''intent => {
                  window.calls=[];window.bytes=[];
                  window.actions.open({id:'a',title:'current selected video',kind:'video',status:'downloaded'},intent);
                }''', intent)
                button = page.locator('[data-share]' if intent == 'share' else '[data-download]')
                expect(button).to_be_enabled()
                button.click()
                expect(page.locator('[role=alert]')).to_have_text('fixture-complete')
                calls = page.evaluate('window.calls')
                assert calls[0]['action'] == intent
                assert calls[-1]['op'] == 'finish'
                received = b''.join(base64.b64decode(part) for part in page.evaluate('window.bytes'))
                assert received == video
                page.locator('[data-close]').click()
            page.evaluate('''() => window.actions.open({id:'missing',title:'missing video',kind:'video',status:'downloaded'},'share')''')
            expect(page.locator('[role=alert]')).to_contain_text('������')
            page.locator('[data-share]').click()
            expect(page.locator('[role=alert]')).to_contain_text('������')
            assert page.locator('[role=alert]').is_visible()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

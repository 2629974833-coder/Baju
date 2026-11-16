"""The real login-probe asset against a controlled password-form server, no live account."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright


def test_password_form_is_visible_before_bundle_and_cookie_survives_switch():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == '/api/capabilities':
                authorized = 'fixture=ok' in self.headers.get('Cookie', '')
                self.send_response(200 if authorized else 401)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'mode': 'real', 'version': 1} if authorized
                                           else {'detail': 'login required'}).encode())
            else:
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(b'<form method="post" action="/login"><input name="password" '
                                 b'type="password"><button>Login</button></form>')

        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            assert body == b'password=fixture-only'
            self.send_response(302)
            self.send_header('Location', '/')
            self.send_header('Set-Cookie', 'fixture=ok; HttpOnly; Path=/; SameSite=Lax')
            self.end_headers()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source = (Path(__file__).resolve().parents[1]
              / 'native/android-share/assets/login-probe.js').read_text(encoding='utf-8')
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            bundled = [False]

            def intercept(route):
                if bundled[0] and route.request.resource_type == 'document':
                    route.fulfill(content_type='text/html', body='<h1>Bundled interface</h1>')
                else:
                    route.continue_()

            page.route('**/*', intercept)
            page.goto(f'http://127.0.0.1:{server.server_port}/')
            assert page.locator('input[type=password]').is_visible()
            page.evaluate(source)
            page.wait_for_function("window.__framekeepAuthState === 'login'")
            assert not bundled[0]
            page.locator('input').fill('fixture-only')
            page.locator('button').click()
            page.wait_for_load_state()
            page.evaluate(source)
            page.wait_for_function("window.__framekeepAuthState === 'ready'")
            bundled[0] = True
            page.reload()
            assert page.locator('h1').inner_text() == 'Bundled interface'
            assert page.request.get(f'http://127.0.0.1:{server.server_port}/api/capabilities').status == 200
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

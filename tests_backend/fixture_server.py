"""Controlled login/media fixture; no external site or account is used."""

import hashlib
import io
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw


@contextmanager
def media_fixture(port=0):
    image = Image.new("RGB", (640, 480), "#175346")
    ImageDraw.Draw(image).text((40, 200), "FRAMEKEEP - LOCAL TEST MEDIA", fill="white")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    photo = buffer.getvalue()
    requests = []
    with tempfile.TemporaryDirectory(prefix="framekeep-fixture-") as temporary:
        video_path = Path(temporary) / "test.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x175346:s=320x240:r=10",
                "-t",
                "1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(video_path),
            ],
            check=True,
        )
        video = video_path.read_bytes()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def send(self, content, mime="text/html; charset=utf-8", status=200, headers=None):
                body = content.encode() if isinstance(content, str) else content
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send(
                    "",
                    status=303,
                    headers={
                        "Location": "/gallery",
                        "Set-Cookie": "fixture_session=authorized-test-session; HttpOnly; SameSite=Lax; Path=/",
                    },
                )

            def do_GET(self):
                authorized = "fixture_session=authorized-test-session" in self.headers.get(
                    "Cookie", ""
                )
                requests.append((self.path, authorized))
                if self.path == "/photo.png" and authorized:
                    return self.send(photo, "image/png")
                if self.path == "/movie.mp4" and authorized:
                    return self.send(video, "video/mp4")
                if self.path in {"/photo.png", "/movie.mp4"}:
                    return self.send("Login required", status=401)
                if self.path == "/gallery" and authorized:
                    return self.send(
                        '<!doctype html><meta charset="utf-8"><title>已授权的测试素材</title><h1>本地测试素材库</h1><p>仅用于验证，不连接外部账号。</p><img alt="测试图片" src="/photo.png" width="640" height="480"><video controls width="320" height="240" src="/movie.mp4"></video>'
                    )
                self.send(
                    '<!doctype html><meta charset="utf-8"><title>Framekeep 受控登录测试</title><style>body{font:24px sans-serif;padding:40px}input,button{font:22px sans-serif;padding:12px;margin:12px;display:block}</style><h1>本地登录测试</h1><p>此为虚构账号测试。可输入任意测试文字后登录。</p><form method="post" action="/login"><input name="user" autofocus placeholder="测试账号"><input name="password" type="password" placeholder="测试密码"><button>登录测试账号</button></form>'
                )

        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield SimpleNamespace(
                url=f"http://127.0.0.1:{server.server_port}",
                photo=photo,
                video=video,
                sha256=hashlib.sha256(photo).hexdigest(),
                requests=requests,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    import uvicorn

    from backend.app import create_app
    from backend.config import Settings

    with media_fixture(4194):
        uvicorn.run(
            create_app(
                Settings(
                    data_dir=Path("test-output/manual-workspace").resolve(),
                    allow_private_hosts={"127.0.0.1"},
                )
            ),
            host="127.0.0.1",
            port=4174,
            access_log=False,
        )

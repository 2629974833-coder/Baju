"""Exercise the Chunlou resolver through the real HLS staging, mux and verification path."""
import subprocess

import httpx
import pytest

from backend.downloader import Downloader
from tests_backend.test_chunlou import OTHER, PAGE, TARGET
from tests_backend.test_downloader import Browser, Policy, control, progress

MEDIA = "https://hls.chunlou.tv"
SOURCE = f"{MEDIA}/hls/{TARGET}/720p/index.m3u8?e=fixture"
KEY_PATH = f"/key/{TARGET}/1?e=1800000000&n=fixture&s=fixture%2Bsig"
KEY_URI = "chunlou://hls.chunlou.tv" + KEY_PATH
KEY_REQUEST = MEDIA + KEY_PATH + "&w=1"


@pytest.fixture(scope="module")
def encrypted_vod(tmp_path_factory):
    folder = tmp_path_factory.mktemp("chunlou-vod")
    key = bytes(range(16))
    (folder / "key").write_bytes(key)
    (folder / "keyinfo").write_text(f"{KEY_URI}\n{folder}/key\n")
    subprocess.run([
        Downloader._binary("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "color=c=blue:s=96x64:r=10:d=3", "-f", "lavfi", "-i", "sine=duration=3",
        "-c:v", "libx264", "-g", "10", "-c:a", "aac", "-f", "hls", "-hls_time", "1",
        "-hls_list_size", "0", "-hls_playlist_type", "vod", "-hls_key_info_file",
        str(folder / "keyinfo"), str(folder / "index.m3u8"),
    ], check=True)
    segments = {SOURCE.split("index.m3u8")[0] + part.name: part.read_bytes() for part in folder.glob("*.ts")}
    return (folder / "index.m3u8").read_bytes(), key, segments


@pytest.fixture
def origin(monkeypatch, encrypted_vod):
    playlist, key, segments = encrypted_vod
    bodies = {SOURCE: playlist, KEY_REQUEST: key, **segments}
    seen = []
    real = httpx.AsyncClient

    def fetch(request):
        address = str(request.url)
        seen.append(address)
        body = bodies.get(address)
        if isinstance(body, str):
            return httpx.Response(302, headers={"Location": body})
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, headers={"Content-Length": str(len(body))}, stream=httpx.ByteStream(body))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(fetch), **kw))
    return bodies, seen


def item(**changes):
    return {
        "id": "asset_chunlou_video", "siteId": "chunlou", "kind": "video", "engine": "yt-dlp",
        "sourceUrl": SOURCE, "pageUrl": PAGE, "_chunlouEpisode": TARGET, "_chunlouDuration": 3.0,
        **changes,
    }


@pytest.mark.parametrize("wrapped", [False, True])
async def test_signed_player_uri_preserves_query_and_exports_complete_mp4(origin, tmp_path, wrapped):
    bodies, seen = origin
    if wrapped:
        bodies[KEY_REQUEST] = bytes.fromhex("0187f4a89059c582753406c9bb0d4e7252")
    result = await Downloader(tmp_path, Browser(), Policy()).download(item(), control(), progress)
    assert seen.count(KEY_REQUEST) == 1
    assert set(seen) == set(bodies)
    assert result["mime"] == "video/mp4"
    assert abs(result["duration"] - 3) < 0.2
    subprocess.run([Downloader._binary("ffmpeg"), "-nostdin", "-v", "error", "-xerror",
                    "-i", str(tmp_path / result["path"]), "-map", "0:v", "-map", "0:a",
                    "-f", "null", "-"], check=True)


@pytest.mark.parametrize("problem", [
    "unmarked", "page", "manifest", "key", "key-redirect", "segment-redirect", "key-to-other-episode",
])
async def test_rejects_mismatched_episode_and_redirect_before_following(origin, tmp_path, problem):
    bodies, seen = origin
    candidate = item()
    forbidden = "https://elsewhere.example/collect"
    if problem == "unmarked":
        del candidate["_chunlouEpisode"]
    elif problem == "page":
        candidate["pageUrl"] = PAGE.replace(TARGET, OTHER)
    elif problem == "manifest":
        candidate["sourceUrl"] = SOURCE.replace(TARGET, OTHER)
        bodies[candidate["sourceUrl"]] = bodies[SOURCE]
    elif problem == "key":
        bodies[SOURCE] = bodies[SOURCE].replace(KEY_URI.encode(), KEY_URI.replace(TARGET, OTHER).encode())
    elif problem == "key-redirect":
        bodies[KEY_REQUEST] = forbidden
    elif problem == "key-to-other-episode":
        forbidden = KEY_REQUEST.replace(TARGET, OTHER)
        bodies[KEY_REQUEST] = forbidden
    else:
        bodies[next(url for url in bodies if url.endswith(".ts"))] = forbidden
    with pytest.raises(ValueError):
        await Downloader(tmp_path, Browser(), Policy()).download(candidate, control(), progress)
    assert forbidden not in seen
    assert not any(f"/key/{OTHER}/" in url for url in seen)
    if problem in {"unmarked", "page", "manifest", "key"}:
        assert KEY_REQUEST not in seen
    assert not list(tmp_path.rglob("original.mp4"))


@pytest.mark.parametrize("reported_by_playlist", [False, True])
async def test_short_output_cannot_replace_previous_episode(origin, tmp_path, reported_by_playlist):
    bodies, _ = origin
    if reported_by_playlist:
        # The scan duration and refreshed EXTINF values agree; the actual mux is still short.
        bodies[SOURCE] = bodies[SOURCE].replace(b"#EXTINF:1.000000", b"#EXTINF:3.000000")
    storage = tmp_path / "assets" / item()["id"]
    storage.mkdir(parents=True)
    original = storage / "original.mp4"
    original.write_bytes(b"previous verified output")
    phases = set()

    async def report(event):
        phases.add(event["phase"])

    with pytest.raises(ValueError, match="时长"):
        await Downloader(tmp_path, Browser(), Policy()).download(
            item(_chunlouDuration=9.0), control(), report
        )
    assert ("verifying" in phases) == reported_by_playlist
    assert original.read_bytes() == b"previous verified output"

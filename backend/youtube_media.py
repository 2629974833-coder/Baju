"""Single YouTube videos through official metadata extraction and guarded transfers.

No remote downloader or plugin is invoked. Signed media URLs remain private asset
fields; playback formats still pass Framekeep's network policy at every request.
"""

import contextlib
import functools
import json
import math
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from http.cookiejar import Cookie
from itertools import islice
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from yt_dlp.extractor.youtube import YoutubeIE
from yt_dlp.extractor.youtube.jsc._builtin.deno import DenoJCP
from yt_dlp.utils import ExtractorError

from backend.selection import (
    audio_metadata,
    choose_audio,
    choose_video,
    normalize_selection,
    observed_language,
)

_PAGE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtube-nocookie.com",
    "youtube-nocookie.com",
}
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}", re.ASCII)
_MAX_FORMATS = 200


@dataclass(frozen=True)
class YouTubeTarget:
    url: str
    post_id: str
    platform: str = "youtube"
    share_url: str | None = None

    @property
    def origin(self):
        return "https://www.youtube.com"


def _https_url(url):
    if (
        not isinstance(url, str)
        or len(url) > 16384
        or any(ord(c) < 32 or ord(c) == 127 for c in url)
    ):
        return None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            return None
        return parsed
    except ValueError:
        return None


def youtube_target(url):
    parsed = _https_url(url)
    if not parsed or parsed.hostname not in _PAGE_HOSTS:
        return None
    path = parsed.path.rstrip("/")
    if parsed.hostname == "youtu.be":
        identifier = path.lstrip("/")
    elif path == "/watch" and "nocookie" not in parsed.hostname:
        values = parse_qs(parsed.query).get("v", [])
        identifier = values[0] if len(values) == 1 else ""
    else:
        match = re.fullmatch(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})", path)
        identifier = match[1] if match else ""
    if not _VIDEO_ID.fullmatch(identifier):
        return None
    return YouTubeTarget(f"https://www.youtube.com/watch?v={identifier}", identifier)


def _media_url(url):
    parsed = _https_url(url)
    return bool(
        parsed
        and (parsed.hostname == "googlevideo.com" or parsed.hostname.endswith(".googlevideo.com"))
    )


def youtube_allowed_request(target, url, method, data):
    """Additional platform scope; it never replaces DNS/IP/redirect validation."""
    parsed = _https_url(url)
    if not parsed or not target or not _VIDEO_ID.fullmatch(target.post_id):
        return False
    if method in {"GET", "HEAD"}:
        return not data and (parsed.hostname in _PAGE_HOSTS or _media_url(url))
    if (
        method != "POST"
        or parsed.hostname != "www.youtube.com"
        or parsed.path != "/youtubei/v1/player"
        or not isinstance(data, bytes)
        or len(data) > 256 * 1024
    ):
        return False
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return False
    allowed_fields = {
        "videoId",
        "context",
        "playbackContext",
        "contentCheckOk",
        "racyCheckOk",
        "serviceIntegrityDimensions",
        "params",
    }
    return (
        isinstance(payload, dict)
        and payload.get("videoId") == target.post_id
        and not set(payload) - allowed_fields
    )


class YouTubeAccessRequired(ExtractorError):
    """Playback requires the visitor's own authorization or verification."""


class YouTubeDRMUnsupported(ExtractorError):
    """No decryption or license retrieval is performed for DRM media."""


class YouTubeUnsupported(ExtractorError):
    """A live, mismatched or unsupported response is not a complete video."""


class YouTubeRuntimeStopped(ExtractorError):
    """A bounded EJS invocation was stopped and its process has been reaped."""


class YouTubeRuntime:
    """Own EJS subprocesses for one scan, including cancellation and output budgets."""

    def __init__(self, *, check=None, deadline=None):
        self.check_external = check
        self.deadline = deadline if deadline is not None else time.monotonic() + 30

    def check(self):
        if self.check_external:
            self.check_external()
        if time.monotonic() >= self.deadline:
            raise YouTubeRuntimeStopped("YouTube 脚本解析超时，请稍后重试", expected=True)

    @staticmethod
    def _stop(process):
        if process.poll() is None:
            if os.name == "posix":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()

    def run_deno(self, provider, stdin, options):
        self.check()
        payload = stdin.encode("utf-8")
        if len(payload) > 16 * 1024**2:
            raise YouTubeRuntimeStopped("YouTube 脚本超过解析大小限制", expected=True)
        # Retain official Deno sandbox flags; never enable filesystem/network access.
        if any(option.startswith(("--allow-", "--allow-all", "-A")) for option in options):
            raise YouTubeRuntimeStopped("YouTube 脚本请求了不支持的运行权限", expected=True)
        command = [provider.runtime_info.path, "run", *options, "-"]
        with tempfile.TemporaryFile() as script:
            script.write(payload)
            script.seek(0)
            self.check()
            process = subprocess.Popen(
                command,
                stdin=script,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=provider._get_env_options(),
                start_new_session=os.name == "posix",
            )
            output, errors = bytearray(), bytearray()
            try:
                with selectors.DefaultSelector() as streams:
                    for stream, buffer in ((process.stdout, output), (process.stderr, errors)):
                        os.set_blocking(stream.fileno(), False)
                        streams.register(stream, selectors.EVENT_READ, buffer)
                    while streams.get_map() or process.poll() is None:
                        self.check()
                        for key, _mask in streams.select(timeout=0.05):
                            chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                            if not chunk:
                                streams.unregister(key.fileobj)
                                continue
                            if len(output) + len(errors) + len(chunk) > 4 * 1024**2:
                                raise YouTubeRuntimeStopped(
                                    "YouTube 脚本输出超过大小限制", expected=True
                                )
                            key.data.extend(chunk)
                self.check()
                stderr = errors.decode("utf-8", errors="replace")
                if hasattr(provider, "_clean_stderr"):
                    stderr = provider._clean_stderr(stderr)
                if process.returncode or stderr.strip():
                    raise YouTubeRuntimeStopped(
                        "YouTube 脚本解析失败，请更新解析组件后重试", expected=True
                    )
                return output.decode("utf-8")
            finally:
                self._stop(process)
                process.stdout.close()
                process.stderr.close()


class YouTubeIE(YoutubeIE):
    def __init__(self, *args, runtime_check=None, runtime_deadline=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._runtime = YouTubeRuntime(check=runtime_check, deadline=runtime_deadline)

    def _real_initialize(self):
        super()._real_initialize()
        for provider in self._jsc_director.providers.values():
            if isinstance(provider, DenoJCP):
                provider._run_deno = functools.partial(self._runtime.run_deno, provider)

    def raise_login_required(self, *args, **kwargs):
        raise YouTubeAccessRequired(
            "YouTube 要求账号权限或人工验证，请在来源站点处理", expected=True
        )

    def report_drm(self, *args, **kwargs):
        raise YouTubeDRMUnsupported("该 YouTube 视频使用 DRM 保护", expected=True)

    def read_post(self, target):
        # extract() initializes official cookie authentication and EJS directors.
        self._runtime.check()
        try:
            info = self.extract(target.url)
        except ExtractorError as error:
            if error.expected and any(
                marker in str(error).lower()
                for marker in (
                    "sign in",
                    "log in",
                    "not a bot",
                    "members-only",
                    "private video",
                    "confirm your age",
                    "join this channel",
                )
            ):
                self.raise_login_required()
            raise
        finally:
            self._runtime.check()
        if not isinstance(info, dict) or info.get("_type") in {
            "url",
            "url_transparent",
            "playlist",
        }:
            raise YouTubeAccessRequired(
                "YouTube 未返回原视频，可能需要权限；不会替换为预告片", expected=True
            )
        if info.get("id") != target.post_id:
            raise YouTubeUnsupported("YouTube 返回的视频与输入链接不符", expected=True)
        return [info]


def youtube_ydl_params():
    # EJS is installed alongside yt-dlp. Runtime fetching/plugins remain disabled.
    sibling = Path(sys.executable).parent / "deno"
    binary = str(sibling) if sibling.is_file() else shutil.which("deno")
    runtime = {"path": binary} if binary else {}
    return {"js_runtimes": {"deno": runtime}, "remote_components": [], "noplaylist": True}


def seed_youtube_cookies(ydl, cookies):
    """Populate only this extraction's in-memory jar for official SAPISID logic."""
    for item in islice(cookies, 500):
        domain = str(item.get("domain") or "").lower()
        if domain.lstrip(".") not in {"youtube.com", "www.youtube.com"}:
            continue
        name, value = str(item.get("name") or ""), str(item.get("value") or "")
        if (
            not name
            or len(name) + len(value) > 16384
            or any(c in name + value for c in "\r\n;\x00")
        ):
            continue
        expires = item.get("expires", -1)
        if not isinstance(expires, (int, float)) or not math.isfinite(expires):
            continue
        if expires > 0 and expires <= time.time():
            continue
        path = item.get("path") or "/"
        if not isinstance(path, str) or not path.startswith("/"):
            continue
        ydl.cookiejar.set_cookie(
            Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=domain.startswith("."),
                domain_initial_dot=domain.startswith("."),
                path=path,
                path_specified=True,
                secure=bool(item.get("secure", False)),
                expires=int(expires) if expires > 0 else None,
                discard=expires <= 0,
                comment=None,
                comment_url=None,
                rest={},
            )
        )


def _number(value):
    return value if type(value) in {int, float} and math.isfinite(value) and value >= 0 else 0


def _format(item):
    if not isinstance(item, dict) or item.get("has_drm") or item.get("fragments"):
        return None
    protocol = item.get("protocol") or "https"
    if protocol not in {"http", "https", "m3u8", "m3u8_native"}:
        return None
    url = item.get("url")
    if not _media_url(url):
        return None
    video, audio = str(item.get("vcodec") or ""), str(item.get("acodec") or "")
    if video != "none" and not video.startswith(("avc1", "h264")):
        return None
    if audio != "none" and not audio.startswith(("mp4a", "aac")):
        return None
    if video == audio == "none" or item.get("ext") not in {"mp4", "m4a"}:
        return None
    manifest = item.get("manifest_url")
    if not protocol.startswith("m3u8") or not _media_url(manifest):
        manifest = None
    return {
        "url": manifest or url,
        "manifestUrl": manifest,
        "formatId": str(item.get("format_id") or "")[:100],
        "protocol": "hls" if protocol.startswith("m3u8") else "http",
        "vcodec": video,
        "acodec": audio,
        "height": int(_number(item.get("height"))),
        "width": int(_number(item.get("width"))),
        "language": observed_language(item.get("language")),
        "bandwidth": _number(item.get("tbr")),
        "filesize": int(_number(item.get("filesize"))),
        "languagePreference": _number(item.get("language_preference")),
    }


def select_youtube_formats(asset, options=None):
    options = normalize_selection(options)
    formats = asset.get("_youtubeFormats") or []
    if not isinstance(formats, list) or len(formats) > _MAX_FORMATS:
        raise ValueError("YouTube 格式目录无效")
    audio = [
        f
        for f in formats
        if f["vcodec"] == "none" and f["acodec"] != "none" and f["protocol"] == "http"
    ]
    candidates = []
    for video in formats:
        if video["vcodec"] == "none":
            continue
        tracks = [video]
        sound_candidates = audio
        if video["protocol"] == "hls":
            sound_candidates = [
                f
                for f in formats
                if f["vcodec"] == "none"
                and f["acodec"] != "none"
                and f["protocol"] == "hls"
                and f.get("manifestUrl")
                and f["manifestUrl"] == video.get("manifestUrl")
            ]
        try:
            sound = choose_audio(
                [video] if video["acodec"] != "none" else sound_candidates,
                options["audioLanguage"],
                language=lambda f: f["language"],
                rank=lambda f: (f["languagePreference"], f["bandwidth"]),
            )
        except ValueError:
            continue
        if sound is None:
            continue
        if sound is not video:
            tracks.append(sound)
        candidates.append(tracks)
    if not candidates and options["audioLanguage"] != "auto":
        raise ValueError("当前视频没有符合指定语言的音轨")
    return choose_video(
        candidates,
        options["maxHeight"],
        height=lambda tracks: tracks[0]["height"],
        rank=lambda tracks: (
            tracks[0]["height"],
            tracks[0]["protocol"] == "hls",
            tracks[0]["bandwidth"],
        ),
    )


def youtube_asset(info, target, headers=None):
    if info.get("has_drm"):
        raise YouTubeDRMUnsupported("该 YouTube 视频使用 DRM 保护", expected=True)
    if (
        info.get("id") != target.post_id
        or info.get("_type") in {"url", "url_transparent", "playlist"}
        or info.get("is_live")
        or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}
    ):
        raise YouTubeUnsupported("当前结果不是该链接的完整点播视频", expected=True)
    duration = _number(info.get("duration"))
    if not duration:
        raise YouTubeUnsupported("未取得 YouTube 原视频完整时长，请稍后重新扫描", expected=True)
    formats = list(islice(info.get("formats") or [], _MAX_FORMATS + 1))
    if len(formats) > _MAX_FORMATS:
        raise YouTubeUnsupported("YouTube 格式目录超过处理上限", expected=True)
    formats = [fmt for item in formats if (fmt := _format(item))]
    item = {"_youtubeFormats": formats}
    try:
        tracks = select_youtube_formats(item)
    except ValueError:
        return None
    safe_headers = {"Referer": target.url}
    for name, value in (headers or {}).items():
        if (
            name.lower() in {"user-agent", "accept", "referer"}
            and isinstance(value, str)
            and len(value) <= 16384
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            safe_headers[name] = value
    item.update(
        sourceUrl=tracks[0]["url"],
        pageUrl=target.url,
        title=str(info.get("title") or "YouTube 视频")[:500],
        kind="video",
        engine="yt-dlp",
        mime="video/mp4",
        _protocol="youtube",
        _resolver="yt-dlp:youtube",
        _formatId="+".join(f["formatId"] for f in tracks),
        _headers=safe_headers,
        width=tracks[0]["width"],
        height=tracks[0]["height"],
        duration=duration,
        _youtubeDuration=duration,
        locked=False,
        category="视频素材",
        shortMedia={
            "platform": "youtube",
            "postId": target.post_id,
            "itemId": target.post_id,
            "watermark": "unknown",
        },
    )
    thumbnail = info.get("thumbnail")
    parsed = _https_url(thumbnail)
    if parsed and (parsed.hostname == "ytimg.com" or parsed.hostname.endswith(".ytimg.com")):
        item["thumbnail"] = thumbnail
    return item


async def _verify_complete(downloader, path, expected):
    output = await downloader._process(
        [
            downloader._binary("ffprobe"),
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            "mov",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        timeout=30,
    )
    try:
        info = json.loads(output)
        streams = info["streams"]
        video = next(
            s
            for s in streams
            if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
        )
        audio = next(s for s in streams if s.get("codec_type") == "audio")
        duration = float(info["format"]["duration"])
        if (
            video.get("codec_name") != "h264"
            or audio.get("codec_name") != "aac"
            or not math.isfinite(duration)
            or duration <= 0
        ):
            raise ValueError()
        if expected and abs(duration - expected) > 1.25:
            raise ValueError()
        for stream in (video, audio):
            length = float(stream.get("duration") or duration)
            if not math.isfinite(length) or length <= 0 or abs(length - duration) > 1.25:
                raise ValueError()
    except (ValueError, KeyError, TypeError, StopIteration):
        raise ValueError("YouTube 成片缺少完整音视频或时长不符，未保存为原件") from None
    return duration


async def download_youtube(downloader, transfer, partial, cache):
    """Reuse guarded HTTP/HLS, durable checkpoints and local-only MP4 muxing."""
    transfer.check()
    asset = transfer.asset
    expected = _number(asset.get("_youtubeDuration"))
    if not expected:
        raise ValueError("缺少 YouTube 原视频完整时长，请重新扫描后下载")
    options = normalize_selection(asset.get("_downloadOptions"))
    tracks = select_youtube_formats(asset, options)
    for track in tracks:
        if not _media_url(track["url"]):
            raise ValueError("YouTube 媒体来源不受支持，请重新扫描")
        await downloader.policy.validate(track["url"])
    if sum(track["filesize"] for track in tracks) > downloader.max_bytes:
        raise ValueError("文件超过大小限制")
    language = tracks[-1]["language"]
    await transfer.progress(
        selection=downloader._selection_info("youtube", options, tracks[0]["height"], language)
    )
    partial.unlink(missing_ok=True)
    if cache.is_symlink():
        cache.unlink()
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if tracks[0]["protocol"] == "hls":
            # A rendition is already selected from observed height/language metadata.
            # Only a master playlist can apply those preferences a second time.
            hls_options = {"maxHeight": None, "audioLanguage": "auto"}
            if tracks[0].get("manifestUrl"):
                hls_options = {**options, "maxHeight": tracks[0]["height"] or options["maxHeight"]}
            transfer.asset = {
                **asset,
                "sourceUrl": tracks[0]["url"],
                "_protocol": "hls",
                "_downloadOptions": hls_options,
            }
            manifest_duration = await downloader._hls(transfer, partial, cache)
            if expected and manifest_duration and abs(expected - manifest_duration) > 1.25:
                raise ValueError("YouTube 播放清单时长与原视频不符")
            expected = manifest_duration or expected
        else:
            objects = cache / "youtube"
            if objects.is_symlink():
                objects.unlink()
            objects.mkdir(mode=0o700, exist_ok=True)
            files, retained = [], set()
            # Retain only this exact selection; signed URL changes safely restart tracks.
            identity = downloader._identity(
                asset.get("siteId"), asset.get("pageUrl"), tracks, options
            )
            for index, track in enumerate(tracks):
                transfer.check()
                key = downloader._identity(identity, index, track["url"])
                retained.add(key)
                path = objects / (key + ".mp4")
                transfer.asset = {**asset, "sourceUrl": track["url"]}
                await downloader._fetch(
                    track["url"],
                    path,
                    transfer,
                    checkpoint=objects / (key + ".json"),
                    identity=key,
                    reuse_complete=True,
                    bindings={"selection": identity},
                )
                files.append(path)
            for path in objects.iterdir():
                if path.stem not in retained:
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
            await transfer.progress(phase="muxing", retryInSeconds=0)
            command = [downloader._binary("ffmpeg"), "-nostdin", "-v", "error"]
            for path in files:
                command += [
                    "-protocol_whitelist",
                    "file,pipe",
                    "-format_whitelist",
                    "mov",
                    "-i",
                    str(path),
                ]
            command += [
                "-map",
                "0:v:0",
                "-map",
                "1:a:0" if len(files) == 2 else "0:a:0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                *audio_metadata(language),
                "-f",
                "mp4",
                str(partial),
            ]
            await downloader._process(command)
        transfer.check()
        await _verify_complete(downloader, partial, expected)
        selection = downloader._selection_info("youtube", options, tracks[0]["height"], language)
        if tracks[0]["protocol"] == "hls" and tracks[0].get("manifestUrl"):
            selection = {**transfer.details.get("selection", selection), "protocol": "youtube"}
        await transfer.progress(selection=selection)
        return expected
    finally:
        transfer.asset = asset

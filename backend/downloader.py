"""Verified media downloads; external tools only receive local, staged media.

HLS and static DASH are staged through the same URL policy as direct downloads.
yt-dlp's DASH parser runs offline; its HLS subprocess receives only staged media.
External media tools never receive browser credentials or remote media URLs.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import signal
import sys
import tempfile
import time
import warnings
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image, UnidentifiedImageError

from . import chunlou
from .chunlou_transport import decode_chunlou_key, playback_key_request
from .dash import MAX_MANIFEST_BYTES, list_mpd_formats, parse_mpd
from .instagram_media import download_instagram, verify_instagram_media
from .network import NetworkResolutionError, is_transient_proxy_error
from .operations import StoragePressure
from .selection import (
    audio_metadata,
    choose_audio,
    choose_video,
    normalize_selection,
    observed_language,
)
from .youtube_media import download_youtube, select_youtube_formats


class DownloadPaused(Exception):
    """Verified partial bytes and private checkpoints are retained for resume."""


class DownloadCancelled(Exception):
    """The operation was cancelled and its temporary data removed."""


class DownloadRetryExhausted(ValueError):
    """A bounded transient retry budget was exhausted; partial data is retained."""


class DownloadAccessRequired(ValueError):
    """The origin requires renewed access; verified partial bytes remain resumable."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"媒体服务器返回 HTTP {status_code}，需要更新访问权限")


class _TransientTransfer(Exception):
    def __init__(self, retry_after=None):
        self.retry_after = retry_after


@dataclass
class _Transfer:
    asset: dict
    control: object
    callback: object
    downloaded: int = 0
    started: float = 0
    network_bytes: int = 0
    total: int = 0
    details: dict = field(
        default_factory=lambda: {
            "phase": "downloading",
            "resumedBytes": 0,
            "retryCount": 0,
            "retryInSeconds": 0,
        }
    )

    def check(self):
        if self.control.cancelled:
            raise DownloadCancelled()
        if self.control.paused:
            raise DownloadPaused()

    async def progress(self, total=None, **details):
        self.check()
        if total is not None:
            self.total = total
        total = self.total
        self.details.update(details)
        await self.callback(
            {
                "progress": min(95, self.downloaded * 95 / total) if total else 0,
                "downloadedBytes": self.downloaded,
                "totalBytes": total,
                "speedBytes": int(self.network_bytes / max(0.01, time.monotonic() - self.started)),
                **self.details,
            }
        )
        self.check()


class Downloader:
    def __init__(self, data_dir: Path, browser, policy, max_bytes: int = 2 * 1024**3, *, storage_guard=None):
        self.data_dir = Path(data_dir).resolve()
        self.browser = browser
        self.policy = policy
        self.max_bytes = max_bytes
        self.storage_guard = storage_guard

    async def download(self, asset: dict, control, on_progress) -> dict:
        if self.storage_guard:
            self.storage_guard.check()
        identifier = asset.get("id", "")
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", identifier
        ):
            raise ValueError("素材 ID 无效")
        if asset.get("kind") not in {"image", "video"}:
            raise ValueError("仅支持图片和视频")
        asset = {**asset, "_downloadOptions": normalize_selection(asset.get("_downloadOptions"))}
        folder = self.data_dir / "assets" / identifier
        if not folder.resolve().is_relative_to(self.data_dir):
            raise ValueError("素材路径无效")
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder.chmod(0o700)
        partial, cache = folder / "original.part", folder / ".hls"
        transfer = _Transfer(asset, control, on_progress, started=time.monotonic())
        operation = asyncio.create_task(self._download(transfer, partial, cache))
        preserve = False
        try:
            while not operation.done():
                transfer.check()
                if self.storage_guard:
                    self.storage_guard.check()
                await asyncio.wait({operation}, timeout=0.1)
            result, extension = await operation
            transfer.check()
            if self.storage_guard:
                self.storage_guard.check()
            destination = folder / ("original" + extension)
            os.replace(partial, destination)
            result["path"] = destination.relative_to(self.data_dir).as_posix()
            await on_progress(
                {
                    **transfer.details,
                    "phase": "completed",
                    "progress": 100,
                    "downloadedBytes": result["size"],
                    "totalBytes": result["size"],
                }
            )
            return result
        except (
            DownloadPaused,
            DownloadRetryExhausted,
            DownloadAccessRequired,
            StoragePressure,
            asyncio.CancelledError,
        ):
            preserve = True
            raise
        except OSError:
            raise ValueError("磁盘空间不足或无法写入下载文件") from None
        finally:
            if not operation.done():
                operation.cancel()
            with contextlib.suppress(BaseException):
                await operation
            if not preserve:
                self._clean(partial, cache)
            elif partial.exists() and not partial.stat().st_size:
                partial.unlink()

    def discard_partial(self, identifier: str) -> None:
        """Remove a paused task's temporary data without touching verified originals."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", identifier):
            raise ValueError("素材 ID 无效")
        folder = self.data_dir / "assets" / identifier
        if not folder.resolve().is_relative_to(self.data_dir):
            raise ValueError("素材路径无效")
        self._clean(folder / "original.part", folder / ".hls")

    @staticmethod
    def _clean(partial, cache):
        partial.unlink(missing_ok=True)
        (partial.parent / ".download.json").unlink(missing_ok=True)
        if cache.is_symlink():
            cache.unlink()
        elif cache.exists():
            shutil.rmtree(cache)

    @staticmethod
    def _protocol(asset):
        if asset.get("engine", "http") != "yt-dlp" or asset.get("kind") != "video":
            return "http"
        if asset.get("_protocol") in {"youtube", "instagram"}:
            return asset["_protocol"]
        if (
            asset.get("_protocol") in {"dash", "http_dash_segments"}
            or asset.get("mime", "").split(";", 1)[0].lower() == "application/dash+xml"
            or urlsplit(asset["sourceUrl"]).path.lower().endswith(".mpd")
        ):
            return "dash"
        return "hls"

    async def describe_options(self, asset):
        """Read only bounded manifest metadata through the normal authorized transport."""
        protocol = self._protocol(asset)
        fixed = {
            "protocol": protocol,
            "selectable": False,
            "heights": [],
            "audioTracks": [],
            "message": "固定源文件，不执行画质转换或音轨替换",
        }
        if protocol in {"http", "instagram"}:
            return fixed
        if protocol == "youtube":
            # Only metadata already parsed for this video is exposed; signed URLs stay private.
            formats = asset.get("_youtubeFormats", [])
            select_youtube_formats(asset)
            heights = sorted({int(fmt["height"]) for fmt in formats
                              if isinstance(fmt.get("height"), (int, float)) and fmt["height"] > 0})
            languages = sorted({str(fmt.get("language") or "und") for fmt in formats
                                if fmt.get("acodec") != "none"})
            return {**fixed, "selectable": True, "heights": heights,
                    "audioTracks": [{"language": lang, "name": lang, "default": False}
                                    for lang in languages],
                    "message": "选择可用画质后合并完整音视频，不添加水印"}
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

        async def progress(_values):
            pass

        transfer = _Transfer(
            asset,
            SimpleNamespace(paused=False, cancelled=False),
            progress,
            started=time.monotonic(),
        )
        async with asyncio.timeout(15):
            with tempfile.TemporaryDirectory(prefix=".inspect-", dir=self.data_dir) as temporary:
                path = Path(temporary) / "manifest"
                final_url, _mime = await self._fetch(
                    asset["sourceUrl"], path, transfer, limit=MAX_MANIFEST_BYTES
                )
                message = "仅列出当前清单声明的选项；具体画质与音轨组合需在下载时确认"
                if protocol == "dash":
                    formats = list_mpd_formats(path.read_bytes(), final_url)
                    heights = [
                        item.get("height") for item in formats if item["_dash_kind"] == "video"
                    ]
                    audio = [
                        {
                            "language": observed_language(item.get("language")),
                            "name": observed_language(item.get("language")),
                            "default": False,
                        }
                        for item in formats
                        if item["_dash_kind"] == "audio"
                    ]
                else:
                    variants, tracks = self._hls_choices(self._hls_lines(path), final_url)
                    if not variants:
                        return {
                            **fixed,
                            "message": "当前为固定 HLS 子清单，未提供可选画质或音轨信息",
                        }
                    heights = [item[0].get("_height") for item in variants]
                    groups = {item[0].get("AUDIO") for item in variants}
                    tracks = [item for item in tracks if item.get("GROUP-ID") in groups]
                    if any(not item.get("URI") for item in tracks):
                        message += "；内嵌音轨仅支持自动保留，不列为可选语言"
                    audio = [
                        {
                            "language": observed_language(item.get("LANGUAGE")),
                            "name": self._track_name(item),
                            "default": item.get("DEFAULT") == "YES",
                        }
                        for item in tracks
                        if item.get("URI")
                    ]
                return {
                    "protocol": protocol,
                    "selectable": True,
                    "heights": sorted(
                        {
                            int(height)
                            for height in heights
                            if type(height) in {int, float} and 0 < height < 100000
                        }
                    ),
                    "audioTracks": list(
                        {
                            (item["language"], item["name"], item["default"]): item
                            for item in audio
                        }.values()
                    ),
                    "message": message,
                }

    @staticmethod
    def _selection_info(protocol, options, height=None, language="und", *, applied=True):
        result = {
            "protocol": protocol,
            "height": height,
            "audioLanguage": language,
            "optionsApplied": applied,
        }
        if not applied:
            result["message"] = "固定源文件，不执行画质转换或音轨替换"
        elif options["audioLanguage"] not in {"auto", language}:
            result["message"] = f"请求 {options['audioLanguage']}，使用兼容的同语种音轨 {language}"
        return result

    async def _download(self, transfer, partial, cache):
        transfer.check()
        expected_duration = None
        engine = transfer.asset.get("engine", "http")
        if engine == "http":
            declared = transfer.asset.get("_shortDuration") or transfer.asset.get("_instagramDuration")
            if type(declared) in (int, float) and math.isfinite(declared) and declared > 0:
                expected_duration = declared
            await transfer.progress(
                selection=self._selection_info(
                    "http", transfer.asset["_downloadOptions"], applied=False
                )
            )
            await self._fetch(
                transfer.asset["sourceUrl"],
                partial,
                transfer,
                media=True,
                checkpoint=partial.parent / ".download.json",
                identity=self._identity(
                    transfer.asset.get("siteId"), engine, transfer.asset["sourceUrl"]
                ),
            )
        elif engine == "yt-dlp" and transfer.asset["kind"] == "video":
            partial.unlink(missing_ok=True)
            if cache.is_symlink():
                cache.unlink()
            cache.mkdir(mode=0o700, exist_ok=True)
            cache.chmod(0o700)
            if self._protocol(transfer.asset) == "youtube":
                expected_duration = await download_youtube(self, transfer, partial, cache)
            elif self._protocol(transfer.asset) == "instagram":
                expected_duration = await download_instagram(self, transfer, partial, cache)
            elif self._protocol(transfer.asset) == "dash":
                await self._dash(transfer, partial, cache)
            else:
                expected_duration = await self._hls(transfer, partial, cache)
        else:
            raise ValueError("不支持此下载引擎或媒体格式")
        transfer.check()
        await transfer.progress(phase="verifying", retryInSeconds=0)
        if transfer.asset["kind"] == "image":
            result, extension = await asyncio.to_thread(self._inspect_image, partial)
        else:
            result, extension = await self._inspect_video(partial)
            if engine == "http" and transfer.asset.get("_instagramAudioExpected") is True:
                await verify_instagram_media(self, partial, expected_duration)
            if expected_duration is not None and abs(result["duration"] - expected_duration) > 1.25:
                raise ValueError("成片时长与目标集的完整播放清单不符，原件未替换")
        selection = dict(transfer.details.get("selection") or {})
        selection["height"] = result.get("height") if transfer.asset["kind"] == "video" else None
        cap = transfer.asset["_downloadOptions"]["maxHeight"]
        if selection.get("optionsApplied") and cap and result.get("height", 0) > cap:
            raise ValueError("实际视频分辨率超过指定画质上限，原件未替换")
        result["selection"] = selection
        transfer.details["selection"] = selection
        size = partial.stat().st_size
        if not size or size > self.max_bytes:
            raise ValueError("文件为空或超过大小限制")
        result.update(size=size, sha256=await asyncio.to_thread(self._sha256, partial))
        return result, extension

    @staticmethod
    def _origin(url):
        parts = urlsplit(url)
        return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)

    async def _headers(self, url, transfer):
        target = urlsplit(url)
        headers = {"User-Agent": "Framekeep/1.0", "Accept-Encoding": "identity"}
        source_origin = self._origin(transfer.asset["sourceUrl"])
        allowed = {
            name.lower(): name
            for name in ("Accept", "User-Agent", "Referer", "Origin", "Authorization")
        }
        for name, value in (transfer.asset.get("_headers") or {}).items():
            if (
                name.lower() in allowed
                and isinstance(value, str)
                and "\r" not in value
                and "\n" not in value
            ):
                if name.lower() != "authorization" or self._origin(url) == source_origin:
                    headers[allowed[name.lower()]] = value
        cookies = await self.browser.cookies_for(transfer.asset["siteId"], url)
        scoped = []
        for cookie in cookies:
            domain = cookie.get("domain", "")
            host = (target.hostname or "").lower()
            domain_match = host == domain.lstrip(".").lower() or (
                domain.startswith(".") and host.endswith(domain.lower())
            )
            path = cookie.get("path", "/")
            request_path = target.path or "/"
            path_match = request_path == path or request_path.startswith(path.rstrip("/") + "/")
            expires = cookie.get("expires", -1)
            if (
                not domain_match
                or not path_match
                or (cookie.get("secure") and target.scheme != "https")
            ):
                continue
            if expires and expires > 0 and expires <= time.time():
                continue
            name, value = str(cookie.get("name", "")), str(cookie.get("value", ""))
            if name and not any(c in name + value for c in "\r\n;"):
                scoped.append(f"{name}={value}")
        if scoped:
            headers["Cookie"] = "; ".join(scoped)
        return headers

    @staticmethod
    def _identity(*parts):
        return hashlib.sha256(json.dumps(parts, ensure_ascii=True).encode()).hexdigest()

    @staticmethod
    def _write_checkpoint(path, state):
        """Publish metadata only after the corresponding bytes were fsynced."""
        temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as output:
                json.dump(state, output, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_checkpoint(checkpoint, path, identity, limit):
        """Verify the saved prefix; a crash may have left an uncommitted tail."""
        digest = hashlib.sha256()
        if not checkpoint or path.is_symlink() or checkpoint.is_symlink():
            return {}, digest
        try:
            if checkpoint.stat().st_size > 16384:
                return {}, digest
            state = json.loads(checkpoint.read_text())
            offset = state.get("offset")
            file_size = path.stat().st_size
            if (
                state.get("version") != 1
                or state.get("identity") != identity
                or type(offset) is not int
                or not 0 < offset <= limit
                or file_size < offset
                or not isinstance(state.get("validator"), dict)
                or type(state.get("total")) is not int
                or state["total"] < offset
                or type(state.get("complete")) is not bool
                or (state["complete"] and not file_size == offset == state["total"])
            ):
                return {}, digest
            validator = state["validator"]
            if not Downloader._valid_validator(validator):
                return {}, digest
            with path.open("rb") as source:
                remaining = offset
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        return {}, hashlib.sha256()
                    digest.update(chunk)
                    remaining -= len(chunk)
            if digest.hexdigest() != state.get("sha256"):
                return {}, hashlib.sha256()
            return state, digest
        except (OSError, ValueError, TypeError, AttributeError):
            return {}, hashlib.sha256()

    @staticmethod
    def _valid_validator(validator):
        value = validator.get("value")
        if not isinstance(value, str) or "\r" in value or "\n" in value:
            return False
        if validator.get("kind") == "etag":
            return bool(re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]*"', value))
        if validator.get("kind") == "modified":
            try:
                return parsedate_to_datetime(value).tzinfo is not None
            except (ValueError, TypeError, OverflowError):
                pass
        return False

    @staticmethod
    def _validator(headers):
        etag = {"kind": "etag", "value": headers.get("etag")}
        if Downloader._valid_validator(etag):
            return etag
        if "etag" in headers:
            return {}  # If-Range dates require absence of any entity tag, including weak ones.
        # RFC 9110: a client can treat Last-Modified as strong only when the
        # response Date is at least 60 seconds later. Weak ETags are never used.
        modified = headers.get("last-modified")
        try:
            date = parsedate_to_datetime(headers.get("date", ""))
            timestamp = parsedate_to_datetime(modified or "")
            if date.tzinfo and timestamp.tzinfo and (date - timestamp).total_seconds() >= 60:
                return {"kind": "modified", "value": modified}
        except (ValueError, TypeError, OverflowError):
            pass
        return {}

    @staticmethod
    def _retry_delay(value, attempt):
        delay = min(0.25 * 2**attempt, 5.0)
        if value:
            try:
                requested = (
                    float(value)
                    if str(value).isdigit()
                    else (parsedate_to_datetime(value).timestamp() - time.time())
                )
                if math.isfinite(requested):
                    delay = min(5.0, max(delay, requested))
            except (ValueError, TypeError, OverflowError):
                pass
        return delay

    async def _fetch(
        self,
        url,
        path,
        transfer,
        *,
        media=False,
        limit=None,
        checkpoint=None,
        identity=None,
        reuse_complete=False,
        bindings=None,
    ):
        """Bounded transfers; every request/redirect re-enters policy and cookie scope."""
        limit = min(limit or self.max_bytes, self.max_bytes)
        base = transfer.downloaded
        resumed_before = transfer.details["resumedBytes"]
        async with httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(20, connect=10),
            proxy=getattr(self.policy, "proxy_url", None),
        ) as client:
            for attempt in range(4):
                try:
                    return await self._fetch_attempt(
                        client,
                        url,
                        path,
                        transfer,
                        base=base,
                        limit=limit,
                        media=media,
                        checkpoint=checkpoint,
                        identity=identity,
                        reuse_complete=reuse_complete,
                        resumed_before=resumed_before,
                        bindings=bindings,
                    )
                except (
                    _TransientTransfer,
                    NetworkResolutionError,
                    httpx.ProxyError,
                    httpx.NetworkError,
                    httpx.TimeoutException,
                    httpx.RemoteProtocolError,
                ) as error:
                    if isinstance(error, httpx.ProxyError) and not is_transient_proxy_error(error):
                        raise
                    if attempt == 3:
                        raise DownloadRetryExhausted(
                            "媒体传输不完整或网络连接失败，重试次数已用尽；可稍后继续"
                        ) from None
                    delay = self._retry_delay(getattr(error, "retry_after", None), attempt)
                    await transfer.progress(
                        phase="retrying",
                        retryCount=transfer.details["retryCount"] + 1,
                        retryInSeconds=delay,
                    )
                    deadline = time.monotonic() + delay
                    while time.monotonic() < deadline:
                        transfer.check()
                        await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
                    transfer.check()

    async def _fetch_attempt(
        self,
        client,
        source,
        path,
        transfer,
        *,
        base,
        limit,
        media,
        checkpoint,
        identity,
        reuse_complete,
        resumed_before,
        bindings,
    ):
        state, digest = await asyncio.to_thread(
            self._read_checkpoint, checkpoint, path, identity, limit
        )
        offset = state.get("offset", 0)
        transfer.downloaded = base + offset
        if media:
            transfer.total = state.get("total", 0)
        if not state:
            path.unlink(missing_ok=True)
            if checkpoint:
                checkpoint.unlink(missing_ok=True)
        url, reset, redirects = source, False, 0
        for _ in range(12):  # Ten redirects plus at most one safe full restart.
            transfer.check()
            url = await self.policy.validate(url)
            if urlsplit(url).scheme not in {"http", "https"}:
                raise ValueError("仅允许 HTTP 和 HTTPS 下载")
            headers = await self._headers(url, transfer)
            conditional = bool(offset and reuse_complete and state.get("complete"))
            if offset:
                validator = state["validator"]
                if conditional:
                    headers[
                        "If-None-Match" if validator["kind"] == "etag" else "If-Modified-Since"
                    ] = validator["value"]
                else:
                    headers.update(Range=f"bytes={offset}-", **{"If-Range": validator["value"]})
            client.cookies.clear()
            async with client.stream("GET", url, headers=headers) as response:
                status = response.status_code
                if status in {301, 302, 303, 307, 308}:
                    if redirects >= 10:
                        raise ValueError("重定向次数超过限制")
                    redirects += 1
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("重定向缺少目标地址")
                    url = urljoin(url, location)
                    if transfer.asset.get("_chunlouEpisode") and (
                        self._origin(url) != self._origin(source)
                        or urlsplit(url).path != urlsplit(source).path
                    ):
                        raise ValueError("春楼播放资源跳转到其他来源或路径，请重新扫描本集")
                    continue
                if status in {408, 429} or 500 <= status <= 599:
                    raise _TransientTransfer(response.headers.get("retry-after"))
                if status in {401, 402, 403}:
                    raise DownloadAccessRequired(status)
                validator = self._validator(response.headers)
                same_resource = state.get("finalIdentity") == self._identity(url)
                if (
                    status == 304
                    and conditional
                    and same_resource
                    and validator == state["validator"]
                ):
                    if base + offset > self.max_bytes:
                        raise ValueError("文件超过大小限制")
                    transfer.details["resumedBytes"] = resumed_before + offset
                    await transfer.progress(
                        phase="resuming",
                        resumeSupported=True,
                        retryInSeconds=0,
                        resumeMessage="已校验并复用完整片段",
                    )
                    return url, state.get("mime", "")
                span = None
                if status == 206 and offset and not conditional:
                    match = re.fullmatch(
                        r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", "")
                    )
                    if match:
                        first, last, total = map(int, match.groups())
                        if (
                            first == offset
                            and last == total - 1
                            and first <= last
                            and total == state["total"]
                            and same_resource
                            and validator == state["validator"]
                        ):
                            span = total - offset
                if status in {206, 304, 416} and span is None:
                    if offset and not reset:
                        # An ambiguous response never authorizes appending or success.
                        offset, state, digest = 0, {}, hashlib.sha256()
                        transfer.downloaded = base
                        path.unlink(missing_ok=True)
                        if checkpoint:
                            checkpoint.unlink(missing_ok=True)
                        reset, url = True, source
                        await transfer.progress(
                            phase="downloading",
                            resumeSupported=False,
                            resumeMessage="服务器续传响应无法校验，已安全重新下载",
                        )
                        continue
                    raise ValueError("媒体服务器返回无效的范围响应")
                if status not in {200, 206}:
                    raise ValueError(f"媒体服务器返回 HTTP {status}")
                mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if mime in {"text/html", "application/xhtml+xml", "application/json"}:
                    raise ValueError("服务器返回登录页或错误页，未保存为媒体")
                if (
                    media
                    and mime
                    and not (
                        mime.startswith(("image/", "video/")) or mime == "application/octet-stream"
                    )
                ):
                    raise ValueError("响应不是支持的图片或视频类型")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("服务器返回不支持的压缩传输编码")
                try:
                    length = int(response.headers.get("content-length", "0"))
                except ValueError:
                    raise ValueError("无效的媒体 Content-Length") from None
                if (
                    status == 200
                    and conditional
                    and same_resource
                    and validator.get("kind") == "etag"
                    and validator == state["validator"]
                ):
                    # Some origins ignore If-None-Match. A matching strong ETag
                    # still identifies the same bytes (RFC 9110 8.8.1), but only
                    # a complete, locally verified object can skip this body.
                    if "content-length" in response.headers and length != offset:
                        raise ValueError("媒体响应长度与已校验完整片段不一致")
                    if base + offset > self.max_bytes:
                        raise ValueError("文件超过大小限制")
                    transfer.details["resumedBytes"] = resumed_before + offset
                    await transfer.progress(
                        phase="resuming",
                        resumeSupported=True,
                        retryInSeconds=0,
                        resumeMessage="已校验并复用完整片段",
                    )
                    return url, mime
                if status == 200:
                    if offset:
                        await transfer.progress(
                            phase="downloading",
                            resumeSupported=False,
                            resumeMessage="源文件已变化或服务器不支持续传，已安全重新下载",
                        )
                    offset, digest = 0, hashlib.sha256()
                    transfer.downloaded = base
                elif "content-length" in response.headers and length != span:
                    raise ValueError("媒体 Content-Range 与 Content-Length 不一致")
                total = offset + (span if span is not None else length)
                if length < 0 or total > limit or base + total > self.max_bytes:
                    raise ValueError("文件超过大小限制")
                state = {
                    "version": 1,
                    "identity": identity,
                    "finalIdentity": self._identity(url),
                    "validator": validator,
                    "total": total,
                    "offset": offset,
                    "sha256": digest.hexdigest(),
                    "complete": False,
                    "mime": mime,
                    "bindings": bindings or {},
                }
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                count, last_saved, completed = offset, offset, False
                with path.open("r+b" if offset else "wb") as output:
                    path.chmod(0o600)
                    output.truncate(offset)
                    output.seek(offset)

                    def save():
                        if checkpoint and count:
                            output.flush()
                            os.fsync(output.fileno())
                            state.update(
                                offset=count, sha256=digest.hexdigest(), complete=completed
                            )
                            self._write_checkpoint(checkpoint, state)

                    try:
                        transfer.details["resumedBytes"] = resumed_before + offset
                        resume_message = "源站未提供可靠续传验证器"
                        if validator:
                            if total:
                                resume_message = "支持校验后续传"
                            elif reuse_complete:
                                resume_message = "源站未声明片段长度，完整片段可校验后复用"
                            else:
                                resume_message = "源站未声明文件长度，当前断点需重新下载"
                        if offset:
                            resume_message = "已校验断点，继续传输"
                        await transfer.progress(
                            total if media else 0,
                            phase="resuming" if offset else "downloading",
                            resumeSupported=bool(validator and (total or reuse_complete)),
                            retryInSeconds=0,
                            resumeMessage=resume_message,
                        )
                        async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                            transfer.check()
                            if (
                                count + len(chunk) > limit
                                or base + count + len(chunk) > self.max_bytes
                            ):
                                raise ValueError("文件超过大小限制")
                            output.write(chunk)
                            count += len(chunk)
                            digest.update(chunk)
                            transfer.downloaded = base + count
                            transfer.network_bytes += len(chunk)
                            if count - last_saved >= 4 * 1024**2:
                                save()
                                last_saved = count
                            await transfer.progress(total if media else 0)
                        if ("content-length" in response.headers and count - offset != length) or (
                            span is not None and count - offset != span
                        ):
                            raise _TransientTransfer()
                        if not count:
                            raise ValueError("服务器返回空媒体")
                        completed = True
                        state["total"] = count
                    finally:
                        save()
                return url, mime
        raise ValueError("重定向次数超过限制")

    @staticmethod
    def _inspect_image(path):
        formats = {
            "JPEG": (".jpg", "image/jpeg"),
            "PNG": (".png", "image/png"),
            "GIF": (".gif", "image/gif"),
            "WEBP": (".webp", "image/webp"),
            "AVIF": (".avif", "image/avif"),
            "BMP": (".bmp", "image/bmp"),
            "TIFF": (".tiff", "image/tiff"),
        }
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    extension, mime = formats[image.format]
                    width, height = image.size
                    image.verify()
                with Image.open(path) as image:
                    image.load()
            return {"mime": mime, "width": width, "height": height, "duration": 0}, extension
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            KeyError,
            Image.DecompressionBombWarning,
            Image.DecompressionBombError,
        ):
            raise ValueError("图片内容无效、损坏或格式不支持") from None

    @staticmethod
    def _binary(name):
        binary = shutil.which(name)
        if not binary and Path("/opt/homebrew/bin", name).is_file():
            binary = str(Path("/opt/homebrew/bin", name))
        if not binary:
            raise ValueError(f"缺少必要媒体工具 {name}")
        return binary

    async def _inspect_video(self, path):
        output = await self._process(
            [
                self._binary("ffprobe"),
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                "mov,matroska,webm,avi,mpegts,mpeg,ogg,flv,asf",
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
            video = next(
                stream
                for stream in info["streams"]
                if stream.get("codec_type") == "video"
                and not stream.get("disposition", {}).get("attached_pic")
            )
            duration = float(info["format"].get("duration") or video.get("duration") or 0)
            width, height = int(video["width"]), int(video["height"])
            if duration <= 0 or not math.isfinite(duration) or width <= 0 or height <= 0:
                raise ValueError()
            if any(
                not stream.get("codec_name")
                for stream in info["streams"]
                if stream.get("codec_type") in {"video", "audio"}
            ):
                raise ValueError()
            container = info["format"]["format_name"].split(",")
            known = {
                "mov": (".mp4", "video/mp4"),
                "webm": (".webm", "video/webm"),
                "matroska": (".mkv", "video/x-matroska"),
                "avi": (".avi", "video/x-msvideo"),
                "mpegts": (".ts", "video/mp2t"),
                "mpeg": (".mpeg", "video/mpeg"),
                "ogg": (".ogv", "video/ogg"),
                "flv": (".flv", "video/x-flv"),
                "asf": (".asf", "video/x-ms-asf"),
            }
            extension, mime = next(known[name] for name in container if name in known)
            return {"mime": mime, "width": width, "height": height, "duration": duration}, extension
        except (ValueError, KeyError, TypeError, StopIteration):
            raise ValueError("视频内容无效、缺少视频轨道或时长") from None

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    async def _process(command, *, timeout=600):
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout)
            if process.returncode:
                raise ValueError("媒体校验或合并失败，源文件可能损坏或格式不受支持")
            return stdout
        except TimeoutError:
            raise ValueError("媒体处理超时") from None
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()

    async def _dash(self, transfer, partial, cache):
        staging, objects = cache / "staging", cache / "objects"
        if staging.is_symlink():
            staging.unlink()
        elif staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(mode=0o700)
        if objects.is_symlink():
            objects.unlink()
        objects.mkdir(mode=0o700, exist_ok=True)
        manifest = staging / "manifest.mpd"
        final_url, _mime = await self._fetch(
            transfer.asset["sourceUrl"], manifest, transfer, limit=MAX_MANIFEST_BYTES
        )
        transfer.check()
        options = transfer.asset["_downloadOptions"]
        tracks = parse_mpd(manifest.read_bytes(), final_url, selection=options)
        language = observed_language(tracks[1].get("language")) if len(tracks) == 2 else "none"
        await transfer.progress(
            selection=self._selection_info("dash", options, tracks[0].get("height"), language)
        )
        # Validate the entire selected resource set before downloading any track.
        # _fetch revalidates each request and redirect, including resumed objects.
        for track in tracks:
            for fragment in track["fragments"]:
                transfer.check()
                await self.policy.validate(fragment["url"])
        source_identity = self._identity(
            transfer.asset.get("siteId"), "dash", transfer.asset["sourceUrl"], options
        )
        manifest_identity = self._identity(final_url, self._sha256(manifest))
        current = {"version": 1, "source": source_identity, "manifest": manifest_identity}
        manifest_checkpoint = cache / "manifest.json"
        try:
            if manifest_checkpoint.stat().st_size > 16384:
                raise ValueError()
            previous = json.loads(manifest_checkpoint.read_text())
        except (OSError, ValueError):
            previous = {}
        if previous != current:
            shutil.rmtree(objects)
            objects.mkdir(mode=0o700)
        self._write_checkpoint(manifest_checkpoint, current)
        transfer.details.update(
            completedSegments=0,
            totalSegments=sum(len(track["fragments"]) for track in tracks),
        )
        used_objects, outputs = set(), []
        for index, track in enumerate(tracks):
            representation = self._identity(track)
            bindings = {source_identity: manifest_identity, "representation": representation}
            output = staging / f"track{index}.mp4"
            with output.open("wb") as destination:
                output.chmod(0o600)
                for fragment_index, fragment in enumerate(track["fragments"]):
                    identity = self._identity(
                        source_identity,
                        manifest_identity,
                        representation,
                        fragment_index,
                        fragment["url"],
                    )
                    used_objects.add(identity)
                    path = objects / (identity + ".bin")
                    await self._fetch(
                        fragment["url"],
                        path,
                        transfer,
                        checkpoint=objects / (identity + ".json"),
                        identity=identity,
                        reuse_complete=True,
                        bindings=bindings,
                    )
                    with path.open("rb") as source:
                        while chunk := source.read(64 * 1024):
                            transfer.check()
                            destination.write(chunk)
                            await asyncio.sleep(0)
                    await transfer.progress(
                        completedSegments=transfer.details["completedSegments"] + 1
                    )
            outputs.append(output)
        for retained in objects.iterdir():
            if retained.stem not in used_objects:
                retained.unlink()
        await transfer.progress(phase="muxing", retryInSeconds=0)
        command = [self._binary("ffmpeg"), "-nostdin", "-v", "error"]
        for output in outputs:
            command += [
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                "mov,matroska,webm,mpegts,aac",
                "-i",
                str(output),
            ]
        command += [
            "-map",
            "0:v:0",
            "-map",
            "1:a:0" if len(outputs) == 2 else "0:a?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(partial),
        ]
        command[-1:-1] = audio_metadata(language)
        await self._process(command)

    async def _hls(self, transfer, partial, cache):
        files, downloaded, visiting = {}, {}, set()
        episode = transfer.asset.get("_chunlouEpisode")
        if episode and (
            chunlou.episode_route(transfer.asset.get("pageUrl")) != episode
            or not chunlou.manifest_url_matches(transfer.asset["sourceUrl"], episode)
        ):
            raise ValueError("春楼播放页或清单与目标集不符")
        expected_duration = None
        player_keys = {}
        options = transfer.asset["_downloadOptions"]
        staging, objects = cache / "staging", cache / "objects"
        if staging.is_symlink():
            staging.unlink()
        elif staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(mode=0o700)
        if objects.is_symlink():
            objects.unlink()
        objects.mkdir(mode=0o700, exist_ok=True)
        for retained in objects.glob("*.bin"):
            if not retained.with_suffix(".json").is_file():
                retained.unlink()  # A crash before the first checkpoint left no reusable bytes.
        source_identity = self._identity(
            transfer.asset.get("siteId"), "hls", transfer.asset["sourceUrl"], options
        )
        transfer.details.update(completedSegments=0, totalSegments=0)
        used_objects = set()

        async def resource(url, *, manifest=False, limit=None, context=None, bindings=None):
            transfer.check()
            if len(files) >= 10000:
                raise ValueError("HLS 片段数量超过限制")
            key = (url, manifest, context)
            if key in downloaded:
                return downloaded[key]
            name = f"resource_{len(files)}" + (".m3u8" if manifest else ".bin")
            identity = self._identity(source_identity, context, url) if context else None
            path = objects / (identity + ".bin") if identity else staging / (name + ".part")
            checkpoint = objects / (identity + ".json") if identity else None
            if identity:
                used_objects.add(identity)
            files[name] = path
            final_url, _mime = await self._fetch(
                url,
                path,
                transfer,
                limit=2 * 1024**2 if manifest else limit,
                checkpoint=checkpoint,
                identity=identity,
                reuse_complete=bool(identity),
                bindings=bindings,
            )
            path.chmod(0o600)
            downloaded[key] = name, final_url
            return name, final_url

        async def decrypt_resource(name, key, iv):
            if key is None:
                return name
            plaintext_name = f"resource_{len(files)}.bin"
            plaintext_path = staging / (plaintext_name + ".part")
            files[plaintext_name] = plaintext_path
            await self._decrypt_aes(files[name], plaintext_path, key, iv, transfer)
            return plaintext_name

        async def playlist(url, depth=0, parent_identity=None, parent_bindings=None):
            nonlocal expected_duration
            if depth > 5 or url in visiting:
                raise ValueError("HLS 清单循环引用或层级超过限制")
            visiting.add(url)
            name, final_url = await resource(url, manifest=True)
            path = files[name]
            manifest_identity = self._identity(parent_identity, final_url, self._sha256(path))
            scope = self._identity(source_identity, url)
            bindings = {**(parent_bindings or {}), scope: manifest_identity}
            if depth == 0:
                manifest_checkpoint = cache / "manifest.json"
                try:
                    previous = json.loads(manifest_checkpoint.read_text())
                except (OSError, ValueError):
                    previous = {}
                current = {"version": 1, "source": source_identity, "manifest": manifest_identity}
                if previous != current:
                    shutil.rmtree(objects)
                    objects.mkdir(mode=0o700)
                self._write_checkpoint(manifest_checkpoint, current)
            # Invalidate descendants immediately, even if this run pauses before
            # the playlist is fully staged. Unrelated audio/video scopes remain.
            for checkpoint in objects.glob("*.json"):
                try:
                    if checkpoint.stat().st_size > 16384:
                        raise ValueError()
                    retained_bindings = json.loads(checkpoint.read_text()).get("bindings")
                    stale = not isinstance(retained_bindings, dict) or (
                        scope in retained_bindings and retained_bindings[scope] != manifest_identity
                    )
                except (ValueError, OSError, AttributeError):
                    stale = True
                if stale:
                    checkpoint.with_suffix(".bin").unlink(missing_ok=True)
                    checkpoint.unlink(missing_ok=True)
            lines = self._hls_lines(path)
            if episode:
                state, duration = chunlou.vod_duration([line for line in lines if line], final_url, episode)
                if state != "full" or duration <= 0:
                    raise ValueError("春楼播放清单不完整或包含其他剧集的片段")
                scanned = transfer.asset.get("_chunlouDuration", duration)
                if (
                    type(scanned) not in (int, float) or not math.isfinite(scanned)
                    or scanned <= 0 or abs(scanned - duration) > 1.25
                ):
                    raise ValueError("春楼播放清单时长与扫描到的正片不符，请重新扫描本集")
                expected_duration = duration
            for line in lines:
                if line.startswith(
                    (
                        "#EXT-X-SESSION-KEY:",
                        "#EXT-X-BYTERANGE:",
                        "#EXT-X-DEFINE:",
                        "#EXT-X-PART:",
                        "#EXT-X-GAP",
                    )
                ):
                    raise ValueError("不支持此 HLS 扩展（会话密钥、字节范围、变量或低延迟片段）")
                if "URI=" in line and not line.startswith(
                    ("#EXT-X-MAP:", "#EXT-X-KEY:", "#EXT-X-MEDIA:")
                ):
                    raise ValueError("HLS 清单包含不支持的外部资源标签")
                if line.startswith("#EXT-X-KEY:"):
                    attributes = self._hls_attributes(line)
                    if (
                        attributes.get("METHOD") not in {"NONE", "AES-128"}
                        or attributes.get("KEYFORMAT", "identity") != "identity"
                        or (episode and not chunlou._key_supported(attributes, final_url, episode))
                    ):
                        raise ValueError(
                            "不支持此 HLS 加密方式（仅支持普通 AES-128）；不会绕过 DRM"
                        )
            variants, audio = self._hls_choices(lines, final_url)
            if variants:
                selected, selected_url = choose_video(
                    variants,
                    options["maxHeight"],
                    height=lambda item: item[0].get("_height"),
                    rank=lambda item: (
                        (item[0].get("_height") or 0, self._hls_bandwidth(item[0]))
                        if options["maxHeight"] is not None
                        else self._hls_bandwidth(item[0])
                    ),
                )
                audio_group = selected.get("AUDIO")
                matches = [
                    track for track in audio if audio_group and track.get("GROUP-ID") == audio_group
                ]
                track = choose_audio(
                    matches,
                    options["audioLanguage"],
                    language=lambda item: item.get("LANGUAGE"),
                    rank=lambda item: (
                        item.get("DEFAULT") == "YES",
                        item.get("AUTOSELECT") == "YES",
                    ),
                )
                if track and not track.get("URI") and options["audioLanguage"] != "auto":
                    raise ValueError("内嵌音轨无法确认独立语言选择，请使用自动音轨")
                if depth == 0:
                    language = (
                        observed_language(track.get("LANGUAGE"))
                        if track and track.get("URI")
                        else "und"
                    )
                    await transfer.progress(
                        selection=self._selection_info(
                            "hls", options, selected.get("_height"), language
                        )
                    )
                entries = await playlist(selected_url, depth + 1, manifest_identity, bindings)
                if track and track.get("URI"):
                    entries += await playlist(
                        urljoin(final_url, track["URI"]), depth + 1, manifest_identity, bindings
                    )
                if len(entries) > 2:
                    raise ValueError("HLS 嵌套音轨结构不受支持")
                visiting.remove(url)
                return entries
            if depth == 0:
                if options["maxHeight"] is not None or options["audioLanguage"] != "auto":
                    raise ValueError(
                        "固定 HLS 子清单无法确认指定画质或语言，请使用主清单或自动选择"
                    )
                await transfer.progress(
                    selection=self._selection_info("hls", options, applied=False)
                )
            if "#EXT-X-ENDLIST" not in lines:
                raise ValueError("暂不支持直播 HLS，请使用完整的点播清单")
            transfer.details["totalSegments"] += sum(
                bool(line and not line.startswith("#")) for line in lines
            )
            rewritten = []
            key, iv, sequence = None, None, 0
            for line in lines:
                if line and not line.startswith("#"):
                    context = self._identity(
                        manifest_identity,
                        hashlib.sha256(key).hexdigest() if key else None,
                        (iv or sequence.to_bytes(16, "big")).hex(),
                    )
                    local, _ = await resource(
                        urljoin(final_url, line), context=context, bindings=bindings
                    )
                    line = await decrypt_resource(local, key, iv or sequence.to_bytes(16, "big"))
                    sequence += 1
                    await transfer.progress(
                        completedSegments=transfer.details["completedSegments"] + 1
                    )
                elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    try:
                        sequence = int(line.partition(":")[2])
                        if sequence < 0 or sequence >= 2**128:
                            raise ValueError()
                    except ValueError:
                        raise ValueError("HLS 片段序号无效") from None
                elif line.startswith("#EXT-X-KEY:"):
                    attributes = self._hls_attributes(line)
                    if attributes.get("METHOD") == "NONE":
                        key, iv = None, None
                    else:
                        uri = attributes.get("URI")
                        if not uri:
                            raise ValueError("AES-128 清单缺少密钥地址")
                        if urlsplit(uri).scheme == "chunlou":
                            key_url = chunlou.chunlou_key_url(uri, episode) if episode else None
                            if not key_url:
                                raise ValueError("春楼播放密钥与目标集不符")
                            if key_url not in player_keys:
                                player_keys[key_url] = await self._player_key(key_url, staging, transfer)
                            key = player_keys[key_url]
                        else:
                            local, _ = await resource(urljoin(final_url, uri), limit=16)
                            key = files[local].read_bytes()
                        if len(key) != 16:
                            raise ValueError("AES-128 密钥长度无效")
                        value = attributes.get("IV")
                        if value and not re.fullmatch(r"0x[0-9a-fA-F]{1,32}", value):
                            raise ValueError("AES-128 初始化向量无效")
                        iv = int(value, 16).to_bytes(16, "big") if value else None
                    continue  # Only decrypted fragments reach yt-dlp.
                elif line.startswith("#EXT-X-MAP:"):
                    attributes = self._hls_attributes(line)
                    if (
                        not attributes.get("URI")
                        or "BYTERANGE" in attributes
                        or (key and iv is None)
                    ):
                        raise ValueError("不支持此 HLS 初始化片段格式")
                    context = self._identity(
                        manifest_identity,
                        "map",
                        hashlib.sha256(key).hexdigest() if key else None,
                        iv.hex() if iv else None,
                    )
                    local, _ = await resource(
                        urljoin(final_url, attributes["URI"]), context=context, bindings=bindings
                    )
                    local = await decrypt_resource(local, key, iv)
                    line = f'#EXT-X-MAP:URI="{local}"'
                elif line.startswith("#") and line.split(":", 1)[0] not in {
                    "#EXTM3U",
                    "#EXTINF",
                    "#EXT-X-VERSION",
                    "#EXT-X-TARGETDURATION",
                    "#EXT-X-ENDLIST",
                    "#EXT-X-PLAYLIST-TYPE",
                    "#EXT-X-DISCONTINUITY",
                    "#EXT-X-DISCONTINUITY-SEQUENCE",
                    "#EXT-X-PROGRAM-DATE-TIME",
                    "#EXT-X-INDEPENDENT-SEGMENTS",
                }:
                    continue  # Do not forward unknown tags or extractor-specific instructions.
                rewritten.append(line)
            path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
            visiting.remove(url)
            return [name]

        entries = await playlist(transfer.asset["sourceUrl"])
        for retained in objects.iterdir():
            if retained.stem not in used_objects:
                retained.unlink()
        await transfer.progress(phase="muxing", retryInSeconds=0)
        token = secrets.token_urlsafe(24)
        server = await asyncio.start_server(
            lambda reader, writer: self._serve_cache(reader, writer, token, files), "127.0.0.1", 0
        )
        try:
            port = server.sockets[0].getsockname()[1]
            outputs = []
            for index, entry in enumerate(entries):
                await self._process(
                    [
                        sys.executable,
                        "-m",
                        "yt_dlp",
                        "--ignore-config",
                        "--no-playlist",
                        "--restrict-filenames",
                        "--socket-timeout",
                        "10",
                        "--retries",
                        "0",
                        "--fragment-retries",
                        "0",
                        "--abort-on-unavailable-fragments",
                        "--max-filesize",
                        str(self.max_bytes),
                        "--downloader",
                        "m3u8:native",
                        "--no-cache-dir",
                        "--fixup",
                        "never",
                        "--no-continue",
                        "--proxy",
                        "",
                        "--quiet",
                        "--no-warnings",
                        "--no-progress",
                        "--format",
                        "best",
                        "--ffmpeg-location",
                        str(staging / "ffmpeg-disabled"),
                        "--output",
                        str(staging / f"track{index}.%(ext)s"),
                        f"http://127.0.0.1:{port}/{token}/{entry}",
                    ]
                )
                candidates = [
                    path
                    for path in staging.glob(f"track{index}.*")
                    if path.suffix in {".mp4", ".m4a", ".ts", ".aac", ".webm"}
                ]
                if len(candidates) != 1 or candidates[0].stat().st_size > self.max_bytes:
                    raise ValueError("HLS 未生成有效媒体轨道或超过大小限制")
                outputs.append(candidates[0])
            # yt-dlp only concatenates cached fragments. Our own restricted
            # FFmpeg invocation probes the real container and joins A/V tracks.
            command = [self._binary("ffmpeg"), "-nostdin", "-v", "error"]
            for output in outputs:
                command += [
                    "-protocol_whitelist",
                    "file,pipe",
                    "-format_whitelist",
                    "mov,matroska,webm,avi,mpegts,mpeg,ogg,flv,asf,aac",
                    "-i",
                    str(output),
                ]
            command += [
                "-map",
                "0:v:0",
                "-map",
                "1:a:0" if len(outputs) == 2 else "0:a?",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                str(partial),
            ]
            command[-1:-1] = audio_metadata(transfer.details["selection"]["audioLanguage"])
            await self._process(command)
        finally:
            server.close()
            await server.wait_closed()
        return expected_duration

    async def _player_key(self, key_url, staging, transfer):
        # Remove transient responses even if a transfer is paused or fails before muxing.
        with tempfile.TemporaryDirectory(dir=staging) as private:
            destination = Path(private) / "key"
            await self._fetch(playback_key_request(key_url), destination, transfer, limit=17)
            return decode_chunlou_key(key_url, destination.read_bytes())

    @staticmethod
    def _hls_lines(path):
        try:
            lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()]
        except UnicodeError:
            raise ValueError("响应不是支持的 HLS 点播清单") from None
        if not lines or lines[0] != "#EXTM3U":
            raise ValueError("响应不是支持的 HLS 点播清单")
        return lines

    @classmethod
    def _hls_choices(cls, lines, final_url):
        variants, audio, pending = [], [], None
        for line in lines:
            if line.startswith("#EXT-X-STREAM-INF:"):
                pending = cls._hls_attributes(line)
                resolution = re.fullmatch(
                    r"([0-9]{1,5})x([0-9]{1,5})", pending.get("RESOLUTION", "")
                )
                pending["_height"] = int(resolution[2]) if resolution else None
            elif line.startswith("#EXT-X-MEDIA:"):
                attributes = cls._hls_attributes(line)
                if attributes.get("TYPE") == "AUDIO":
                    audio.append(attributes)
            elif line and not line.startswith("#") and pending is not None:
                variants.append((pending, urljoin(final_url, line)))
                pending = None
            if len(variants) + len(audio) > 128:
                raise ValueError("HLS 可选画质或音轨数量超过限制")
        return variants, audio

    @staticmethod
    def _hls_bandwidth(attributes):
        value = attributes.get("BANDWIDTH", "0")
        return int(value) if re.fullmatch(r"[0-9]{1,16}", value) else 0

    @staticmethod
    def _track_name(track):
        # NAME is arbitrary origin text and may contain a signed URL or token.
        # Expose a bounded language label, never the raw manifest field.
        return observed_language(track.get("LANGUAGE"))

    @staticmethod
    def _hls_attributes(line):
        value = line.partition(":")[2]
        return {
            match[1]: match[2] if match[2] is not None else match[3]
            for match in re.finditer(r'([A-Z0-9-]+)=(?:"([^"\r\n]*)"|([^,\r\n]+))', value)
        }

    @staticmethod
    async def _decrypt_aes(source, destination, key, iv, transfer):
        """Ordinary authorized AES-128 HLS only, with a fetched 16-byte key."""
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        unpadder = padding.PKCS7(128).unpadder()
        try:
            with source.open("rb") as encrypted, destination.open("wb") as plaintext:
                destination.chmod(0o600)
                while chunk := encrypted.read(64 * 1024):
                    transfer.check()
                    plaintext.write(unpadder.update(decryptor.update(chunk)))
                    await asyncio.sleep(0)
                plaintext.write(unpadder.update(decryptor.finalize()) + unpadder.finalize())
        except ValueError:
            raise ValueError("AES-128 媒体解密失败，密钥或片段内容无效") from None

    @staticmethod
    async def _serve_cache(reader, writer, token, files):
        """A closed set of cached files, never a forwarding proxy or directory server."""
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            if len(request) > 16384:
                return
            first = request.split(b"\r\n", 1)[0].decode("ascii").split(" ")
            if len(first) != 3 or first[0] not in {"GET", "HEAD"}:
                return
            name = first[1].removeprefix(f"/{token}/")
            path = files.get(name) if first[1].startswith(f"/{token}/") else None
            if path is None:
                writer.write(
                    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            else:
                mime = (
                    "application/vnd.apple.mpegurl"
                    if name.endswith(".m3u8")
                    else "application/octet-stream"
                )
                writer.write(
                    (
                        f"HTTP/1.1 200 OK\r\nContent-Type: {mime}\r\nContent-Length: {path.stat().st_size}"
                        "\r\nConnection: close\r\n\r\n"
                    ).encode()
                )
                if first[0] == "GET":
                    with path.open("rb") as source:
                        while chunk := source.read(64 * 1024):
                            writer.write(chunk)
                            await writer.drain()
            await writer.drain()
        except (
            OSError,
            ValueError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

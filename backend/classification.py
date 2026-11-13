"""Conservative content hints, separate from download integrity and entitlement."""

import re
from urllib.parse import unquote, urlsplit

KINDS = {"preview", "episode", "audio", "unknown"}
_PREVIEW = re.compile(
    r"(?:^|[\W_])(?:previews?|trailers?|teasers?|samples?)(?:$|[\W_])|预告|预览|试看", re.I
)
_EPISODE = re.compile(r"(?:^|[/_. -])(?:episodes?[/_. -]*\d+|s\d+e\d+)(?:$|[/_. -])", re.I)
_AUDIO = re.compile(r"(?:^|[/_.-])(?:stream[_-]?)?audio\d*(?:$|[/_.-])", re.I)
_LUSTFLIX_HOSTS = {"lustflix.tv", "www.lustflix.tv"}
_PLAYBACK_MEDIA = re.compile(
    r"/api/playback-media/[A-Za-z0-9._~-]+/([A-Za-z0-9_-]+)/(\d+)/"
    r"(master|stream|stream_\d+|stream_audio\d+)\.m3u8"
)


def lustflix_stream_matches(source, page):
    """The publisher's Cloudflare Stream entrypoint is bound to the exact show and episode."""
    route = re.fullmatch(r"/watch/([A-Za-z0-9_-]+)/([1-9][0-9]{0,3})/?", page.fragment)
    if not (route and page.scheme == source.scheme == "https" and page.netloc == source.netloc == "lustflix.tv"
            and page.path in {"", "/"} and not page.query and not source.fragment and not source.query):
        return False
    if source.path == f"/api/media/{route[1]}/{route[2]}/stream.m3u8":
        return True
    granted = _PLAYBACK_MEDIA.fullmatch(source.path)
    return bool(granted and granted.groups() == (route[1], route[2], "stream"))


def _result(kind, confidence, reasons, source="automatic"):
    return {
        "kind": kind,
        "confidence": confidence,
        "reasons": reasons,
        "source": source,
        "version": 1,
    }


def _generic_episode_evidence(asset, source, page):
    evidence = asset.get("_episodeEvidence")
    if not isinstance(evidence, dict):
        return False
    series, ordinal = evidence.get("seriesKey"), evidence.get("ordinal")
    if (
        type(evidence.get("version")) is not int
        or evidence["version"] != 1
        or evidence.get("adapterId") != "generic"
        or evidence.get("source") != "observed-main-video"
        or not isinstance(series, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", series)
        or type(ordinal) is not int
        or not 1 <= ordinal <= 1000
        or evidence.get("episodeKey") != f"{series}:{ordinal}"
        or evidence.get("pageUrl") != asset.get("pageUrl")
        or evidence.get("sourceUrl") != asset.get("sourceUrl")
    ):
        return False
    try:
        catalog = urlsplit(evidence.get("catalogUrl", ""))
        for parsed in (source, page, catalog):
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                return False
            parsed.port

        def origin(parsed):
            return (
                parsed.scheme,
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
            )

        return origin(page) == origin(catalog)
    except (TypeError, ValueError, AttributeError):
        return False


def automatic_classification(asset):
    if asset.get("kind") != "video":
        return _result("unknown", "low", ["非视频素材"])
    try:
        source = urlsplit(asset.get("sourceUrl", ""))
        page = urlsplit(asset.get("pageUrl", ""))
        source.port, page.port  # Reject malformed ports before matching known routes.
    except ValueError:
        return _result("unknown", "low", ["来源信息不足"])
    path = unquote(source.path).lower()
    playback = (
        _PLAYBACK_MEDIA.fullmatch(source.path) if source.hostname in _LUSTFLIX_HOSTS else None
    )
    if playback:
        # Opaque signatures are not content labels; keep slug and role evidence.
        path = f"/api/playback-media/{playback[1]}/{playback[2]}/{playback[3]}.m3u8".lower()
    # Do not inspect signed query values or a page title shared by every media item.
    label = str(asset.get("_mediaLabel", ""))[:200]
    if _PREVIEW.search(path) or _PREVIEW.search(label):
        return _result("preview", "high", ["媒体路径或播放器标签明确标记预览 / 预告"])
    mime = str(asset.get("_observedMime", asset.get("mime", ""))).split(";", 1)[0].lower()
    if _AUDIO.search(path) or (mime.startswith("audio/") and "mpegurl" not in mime):
        return _result("audio", "high", ["媒体路径或类型标记独立音轨；不是完整音视频"])
    if _generic_episode_evidence(asset, source, page):
        return _result(
            "episode",
            "medium",
            ["已观察到通用剧集目录关联页面的主视频", "正片候选；完整性和播放权限需单独验证"],
        )
    if source.hostname in _LUSTFLIX_HOSTS and page.hostname in _LUSTFLIX_HOSTS:
        media = re.fullmatch(r"/api/media/[^/]+/(\d+)/(?:master|stream_\d+)\.m3u8", path)
        route = re.fullmatch(r"/watch/[^/]+/(\d+)", page.fragment.rstrip("/"), re.I)
        number = media[1] if media else None
        if playback:
            route = re.fullmatch(r"/watch/[A-Za-z0-9_-]+/(\d+)", page.fragment.rstrip("/"))
            if (
                source.scheme == page.scheme == "https"
                and source.port in {None, 443}
                and page.port in {None, 443}
                and not source.username
                and not source.password
                and not page.username
                and not page.password
            ):
                if playback[3] != "stream":
                    number = playback[2]
        if lustflix_stream_matches(source, page) or (number is not None and route and number == route[1]):
            return _result(
                "episode",
                "medium",
                ["Lustflix 单集播放路由与媒体集号一致", "正片候选；完整性和播放权限需单独验证"],
            )
    if _EPISODE.search(path) or re.search(r"\bfull episode\b|完整单集|正片", label, re.I):
        return _result(
            "episode", "medium", ["媒体自身含单集或正片标记", "正片候选；时长不能证明内容完整"]
        )
    return _result("unknown", "low", ["缺少媒体自身的正片或预览依据；时长不参与判断"])


def classify_asset(asset):
    existing = asset.get("classification")
    if (
        isinstance(existing, dict)
        and existing.get("source") == "manual"
        and existing.get("kind") in KINDS
    ):
        return manual_classification(existing["kind"])
    return automatic_classification(asset)


def manual_classification(kind):
    if kind not in KINDS:
        raise ValueError("未知内容类型")
    return _result(kind, "high", ["用户手动分类；不代表系统已验证内容完整或播放权限"], "manual")

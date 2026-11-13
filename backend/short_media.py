"""Identify one social post and parse its own media using yt-dlp's platform adapters."""

import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from yt_dlp.extractor.instagram import InstagramIE, _id_to_pk
from yt_dlp.extractor.tiktok import DouyinIE, TikTokIE
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import ExtractorError, parse_duration, traverse_obj, urlencode_postdata

from backend.instagram_requests import READ_DOCUMENT_ID, READ_ENDPOINT, READ_QUERY_NAME
from backend.youtube_media import youtube_target

_HOSTS = {
    "tiktok": {"www.tiktok.com", "tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "www.tiktokv.com"},
    "douyin": {"www.douyin.com", "douyin.com", "v.douyin.com", "m.douyin.com", "www.iesdouyin.com", "iesdouyin.com"},
    "instagram": {"www.instagram.com", "instagram.com", "i.instagram.com"},
}
_ORIGINS = {platform: f"https://www.{platform}.com" for platform in _HOSTS}
_MAX_PHOTOS = 50
_MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.3 Mobile/15E148 Safari/604.1"
)


def platform_for_url(url):
    if youtube_target(url):
        return "youtube"
    try:
        p = urlsplit(url)
        if p.scheme not in {"http", "https"} or p.username is not None or p.password is not None:
            return None
        if p.port not in {None, 443 if p.scheme == "https" else 80}:
            return None
        return next((name for name, hosts in _HOSTS.items() if p.hostname in hosts), None)
    except (TypeError, ValueError, AttributeError):
        return None


@dataclass(frozen=True)
class ShortTarget:
    platform: str
    url: str
    post_id: str | None = None
    share_url: str | None = None

    @property
    def origin(self):
        return _ORIGINS[self.platform]


def short_target(url):
    if target := youtube_target(url):
        return target
    platform = platform_for_url(url)
    if not platform:
        return None
    p = urlsplit(url)
    path = p.path.rstrip("/")
    origin = _ORIGINS[platform]
    code = r"[A-Za-z0-9_-]{1,80}"
    if platform == "douyin":
        work = re.fullmatch(r"/(?:share/)?(video|note)/(\d{1,25})", path)
        if work and p.hostname != "v.douyin.com":
            share_url = f"https://{p.hostname}{path}/" if path.startswith("/share/") else None
            return ShortTarget(platform, f"{origin}/{work[1]}/{work[2]}", work[2], share_url)
        modal = parse_qs(p.query).get("modal_id", [])
        if p.hostname in {"www.douyin.com", "douyin.com"} and len(modal) == 1 and re.fullmatch(r"\d{1,25}", modal[0]):
            return ShortTarget(platform, f"{origin}/video/{modal[0]}", modal[0])
        share = p.hostname == "v.douyin.com" and re.fullmatch("/" + code, path)
    elif platform == "tiktok":
        if p.hostname in {"www.tiktok.com", "tiktok.com", "www.tiktokv.com"}:
            work = re.fullmatch(r"/(@[\w.-]{1,80}|share)/(video|photo)/(\d{1,25})", path, re.ASCII)
            if work:
                return ShortTarget(platform, origin + path, work[3])
        share = ((p.hostname in {"vm.tiktok.com", "vt.tiktok.com"} and re.fullmatch("/" + code, path))
                 or (p.hostname in {"www.tiktok.com", "tiktok.com"} and re.fullmatch("/t/" + code, path)))
    else:
        if p.hostname == "i.instagram.com":
            return None
        work = re.fullmatch(r"/(?:[A-Za-z0-9._]+/)?(p|reels?|tv)/(" + code + ")", path)
        if work and not path.startswith("/share/") and work[2] != "audio":
            return ShortTarget(platform, f"{origin}/{work[1]}/{work[2]}/", work[2])
        share = re.fullmatch(r"/share/(?:reel/|p/)?" + code, path)
    if share:
        return ShortTarget(platform, f"https://{p.hostname}{p.path}")
    return None


class ShortAccessRequired(ExtractorError):
    """Signal an access gate without echoing upstream responses, URLs or credentials."""


class ShortMetadataUnavailable(ExtractorError):
    """Missing public metadata is not evidence that an account is required."""

    def __init__(self):
        super().__init__("暂未取得公开媒体，请稍后重试", expected=True)


class ShortMediaUnsupported(ExtractorError):
    """Known incomplete media must not silently fall back to an unverified embed."""


def _instagram_manifest(raw):
    message = "Instagram 音视频描述暂不受支持，未改用可能缺少声音的视频"
    try:
        if (not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > 2 * 1024**2
                or "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper()):
            raise ValueError()
        root = ET.fromstring(raw)
        def name(node):
            return node.tag.rsplit("}", 1)[-1]
        if name(root) != "MPD" or root.get("type", "static") != "static":
            raise ValueError()
        if len([child for child in root if name(child) == "Period"]) != 1:
            raise ValueError()
        pending, count, representations = [(root, 0)], 0, 0
        while pending:
            node, depth = pending.pop()
            count += 1
            if count > 2000 or depth > 16 or name(node) in {
                "ContentProtection", "SegmentTemplate", "SegmentList", "SegmentTimeline", "SegmentURL",
            } or any("xlink" in key.lower() for key in node.attrib):
                raise ValueError()
            if name(node) == "Representation":
                representations += 1
                bases = [child for child in node if name(child) == "BaseURL"]
                if representations > 64 or len(bases) != 1 or not _instagram_track_url(bases[0].text):
                    raise ValueError()
            pending.extend((child, depth + 1) for child in node)
        if not representations:
            raise ValueError()
        return root
    except (ValueError, ET.ParseError, TypeError, AttributeError):
        raise ShortMediaUnsupported(message, expected=True) from None


def _instagram_track_url(value):
    if not isinstance(value, str) or len(value) > 16384 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and bool(parsed.hostname) and parsed.port in {None, 443}
                and parsed.username is None and parsed.password is None and not parsed.fragment)
    except ValueError:
        return False


class _PostAccess:
    def raise_login_required(self, *args, **kwargs):
        raise ShortAccessRequired("请在来源站点登录或验证后重试", expected=True)


def _picture(urls, item_id, title):
    if isinstance(urls, list):
        url = next((u for u in urls if isinstance(u, str) and u.startswith("https://")), None)
        if url:
            return {"kind": "image", "url": url, "itemId": str(item_id or "")[:300],
                    "title": title}


def _douyin_playback_url(url):
    """Try the publisher's sibling playback endpoint, never rewrite arbitrary CDN URLs."""
    try:
        parsed = urlsplit(url)
        video_ids = parse_qs(parsed.query).get("video_id", [])
        if (parsed.scheme == "https" and parsed.hostname == "aweme.snssdk.com"
                and parsed.port in {None, 443} and parsed.username is None and parsed.password is None
                and not parsed.fragment and parsed.path == "/aweme/v1/playwm/"
                and len(video_ids) == 1 and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", video_ids[0])):
            return parsed._replace(path="/aweme/v1/play/").geturl()
    except (TypeError, ValueError, AttributeError):
        pass
    return None


class _ShortTikTokIE(_PostAccess, TikTokIE):
    def read_post(self, target):
        # Only read the addressed document. Login and browser challenges go back to the user.
        page = self._download_webpage(target.url, target.post_id)
        detail = traverse_obj(self._get_universal_data(page, target.post_id), ("webapp.video-detail", {dict})) or {}
        # yt-dlp identifies these as private posts/accounts; 10204 is an IP block.
        if detail.get("statusCode") in (10216, 10222):
            self.raise_login_required()
        if detail.get("statusCode") not in (0, "0", None):
            raise ShortMetadataUnavailable()
        item = traverse_obj(detail, ("itemInfo", "itemStruct", {dict})) or {}
        if not item:
            item = traverse_obj(self._get_sigi_state(page, target.post_id), ("ItemModule", target.post_id, {dict})) or {}
        if not item:
            raise ShortMetadataUnavailable()
        if str(item.get("id")) != target.post_id:
            return []
        photos = traverse_obj(item, ("imagePost", "images", {list})) or []
        if photos:
            addresses = traverse_obj(photos[:_MAX_PHOTOS + 1], (..., "imageURL", {dict}))
            return [_picture(address.get("urlList"), address.get("uri"), item.get("desc") or "作品图片")
                    for address in addresses]
        if item.get("isContentClassified") and not item.get("video"):
            self.raise_login_required()
        info = self._parse_aweme_video_web(item, target.url, target.post_id)
        playback_urls = set(traverse_obj(item, ("video", "bitrateInfo", ..., "PlayAddr", "UrlList", ..., {str})))
        for fmt in info.get("formats", []):
            if fmt.get("url") in playback_urls:
                fmt["_playback"] = True
        return [info]


class _ShortDouyinIE(_PostAccess, DouyinIE):
    def _share_item(self, target, kind, share):
        for attempt in range(2):
            try:
                page, handle = self._download_webpage_handle(
                    share, target.post_id, headers={"User-Agent": _MOBILE_USER_AGENT})
            except ExtractorError as error:
                if isinstance(error.cause, HTTPError) and error.cause.status in {401, 402, 403}:
                    self.raise_login_required()
                return None
            state = self._search_json(r"(?:window\.)?_ROUTER_DATA\s*=", page,
                                      "share page data", target.post_id, default={})
            route = traverse_obj(state, ("loaderData", f"{kind}_(id)/page", {dict})) or {}
            payload = traverse_obj(route, ("videoInfoRes", {dict}))
            if payload is not None:
                if payload.get("status_code") not in (0, "0", None):
                    raise ShortMetadataUnavailable()
                return next((i for i in traverse_obj(payload, ("item_list", ..., {dict}))
                             if str(i.get("aweme_id")) == target.post_id), {})
            # An initial public shell may issue the visitor cookie needed to render
            # this exact work. Read it once more, without executing any challenge.
            if (attempt or str(route.get("itemId")) != target.post_id
                    or "ttwid=" not in handle.headers.get("Set-Cookie", "")):
                return None
        return None

    def read_post(self, target):
        kind = "note" if "/note/" in target.url else "video"
        share = target.share_url or f"https://m.douyin.com/share/{kind}/{target.post_id}/"
        item = self._share_item(target, kind, share)
        from_share = item is not None
        if item is None:
            data = self._download_json("https://www.douyin.com/aweme/v1/web/aweme/detail/", target.post_id,
                                       query={"aweme_id": target.post_id})
            item = traverse_obj(data, ("aweme_detail", {dict})) or {}
        if not item:
            raise ShortMetadataUnavailable()
        if str(item.get("aweme_id")) != target.post_id:
            return []
        photos = traverse_obj(item, ("images", {list})) or []
        if photos:
            return [_picture(p.get("url_list"), p.get("uri"), item.get("desc") or "作品图片")
                    for p in photos[:_MAX_PHOTOS + 1] if isinstance(p, dict)]
        video = traverse_obj(item, ("video", {dict})) or {}
        # The public share payload uses null for missing bitrate variants.
        info = self._parse_aweme_video_app({**item, "video": {**video, "bit_rate": video.get("bit_rate") or []}})
        if from_share:
            info["http_headers"] = {"Referer": share, "User-Agent": _MOBILE_USER_AGENT}
        # Only the addressed download rendition inherits Douyin's explicit watermark flag.
        if video.get("has_watermark") in (False, 0):
            download_urls = traverse_obj(video, ("download_addr", "url_list", ..., {str}))
            for fmt in info.get("formats", []):
                if fmt.get("url") in download_urls:
                    fmt["_sourceClean"] = True
        # Share pages sometimes call a watermarked download a play_addr. Keep
        # it as a fallback; the transport verifies the official sibling endpoint
        # before selection. Its path alone is not proof of watermark-free frames.
        for fmt in list(info.get("formats") or []):
            if candidate := _douyin_playback_url(fmt.get("url")):
                playback = {**fmt, "url": candidate, "format_id": f"{fmt.get('format_id') or 'video'}-play",
                            "format_note": "Playback video (official endpoint)",
                            "_cleanCandidate": True, "_playback": True, "_sourceClean": False}
                playback.pop("filesize", None)
                info["formats"].append(playback)
        return [info]


class _ShortInstagramIE(_PostAccess, InstagramIE):
    @property
    def _is_logged_in(self):
        return self.has_session

    def read_post(self, target):
        self.post_id = target.post_id
        self.photo = None
        try:
            info = self._public_product(target) if not self.has_session else None
            if info is None:
                info = self._real_extract(target.url)
        except ShortAccessRequired:
            raise
        except ExtractorError:
            # The native adapter raises "no video" for a standalone photo.
            if self.photo is None:
                raise
            info = self.photo
        if info.get("id") != target.post_id:
            return []
        return info.get("entries", []) if info.get("_type") == "playlist" else [info]

    def _public_product(self, target):
        # The official logged-out query also works without browser impersonation.
        # Its visitor token lives only in this extraction, never in saved assets.
        page = self._download_webpage(target.origin + "/", target.post_id, fatal=False)
        if not page:
            return None
        eqmc = self._search_json(r'<script\b[^>]*\bid=["\']__eqmc["\'][^>]*>', page,
                                 "visitor metadata", target.post_id, end_pattern=r'</script>', default={})
        lsd = traverse_obj(eqmc, ("l", {str})) or self._search_regex(
            r'\["LSD",\[\],\{"token":"([^"]+)"', page, "visitor token", default=None)
        if not lsd or len(lsd) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in lsd):
            return None
        headers = {**self._api_headers, "X-FB-Friendly-Name": READ_QUERY_NAME, "X-FB-LSD": lsd,
                   "X-Requested-With": "XMLHttpRequest", "Referer": target.url}
        csrf = self._get_cookies(target.origin).get("csrftoken")
        if csrf:
            headers["X-CSRFToken"] = csrf.value
        response = self._download_json(READ_ENDPOINT, target.post_id, fatal=False,
                                       headers=headers, data=urlencode_postdata({
                                           "lsd": lsd, "fb_api_caller_class": "RelayModern",
                                           "fb_api_req_friendly_name": READ_QUERY_NAME, "server_timestamps": "true",
                                           "variables": json.dumps({"media_id": str(_id_to_pk(target.post_id))}, separators=(",", ":")),
                                           "doc_id": READ_DOCUMENT_ID,
                                       }))
        product = traverse_obj(response, ("data", "xig_polaris_media", "if_not_gated_logged_out", {dict}))
        return self._extract_product(product, get_comments=False) if product else None

    def _extract_product(self, product_info, *args, **kwargs):
        info = super()._extract_product(product_info, *args, **kwargs)
        if info.get("id") == self.post_id and info.get("kind") == "image":
            self.photo = info
        return info

    def read_embed(self, target):
        # Some public posts expose their media only in Instagram's own embed.
        page = self._download_webpage(f"{target.origin}/p/{target.post_id}/embed/", target.post_id, fatal=False)
        if not page:
            return []
        for match in re.finditer(r'"contextJSON"\s*:\s*', page):
            try:
                block, _ = json.JSONDecoder().raw_decode(page, match.end())
                obj = json.loads(block) if isinstance(block, str) else block
            except (ValueError, RecursionError):
                continue
            if not isinstance(obj, dict) or not isinstance(obj.get("gql_data"), dict):
                continue
            media = obj["gql_data"].get("shortcode_media")
            if not isinstance(media, dict) or media.get("shortcode") != target.post_id:
                continue
            if media.get("__typename") == "GraphSidecar":
                edges = traverse_obj(media, ("edge_sidecar_to_children", "edges", {list})) or []
                children = [edge.get("node") for edge in edges[:_MAX_PHOTOS + 1] if isinstance(edge, dict)]
            else:
                children = [media]
            results, seen = [], set()
            for item in children:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                item_id = str(item["id"])
                if item_id in seen:
                    continue
                title = item.get("accessibility_caption") or "作品内容"
                if item.get("is_video") is True and item.get("video_url"):
                    video = self._extract_product_media({
                        **item, "pk": item_id, "media_type": 2,
                        "video_versions": [{"id": "embed", "url": item["video_url"]}],
                    })
                    results.append({**video, "id": item_id, "title": title,
                                    "thumbnail": item.get("display_url")})
                    seen.add(item_id)
                elif item.get("__typename") == "GraphImage" and item.get("is_video") is False:
                    resources = traverse_obj(item, ("display_resources", {list})) or []
                    photos = [r for r in resources[:_MAX_PHOTOS] if isinstance(r, dict)]
                    photos.sort(key=lambda r: r.get("config_width") if isinstance(r.get("config_width"), int) else 0,
                                reverse=True)
                    photo = _picture([r.get("src") for r in photos] + [item.get("display_url")], item_id, title)
                    if photo:
                        results.append(photo)
                        seen.add(item_id)
            return results
        return []

    def _extract_product_media(self, media):
        raw = media.get("video_dash_manifest") or traverse_obj(media, ("dash_info", "video_dash_manifest", {str}))
        document = _instagram_manifest(raw) if raw else None
        if raw:
            media = {**media, "video_dash_manifest": raw}
        info = super()._extract_product_media(media)
        if media.get("media_type") != 1:
            formats = info.get("formats") or []
            expected_audio = media.get("has_audio") if type(media.get("has_audio")) is bool else None
            duration = info.get("duration") or (parse_duration(document.get("mediaPresentationDuration")) if document is not None else None)
            if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
                duration = None
            audio = [f for f in formats if f.get("_instagramDash") and f.get("vcodec") == "none"
                     and str(f.get("acodec") or "").startswith(("mp4a", "aac"))]
            if document is not None and expected_audio is not False and not audio:
                raise ShortMediaUnsupported("Instagram 清单未提供可合并的原始音轨，未保存静音替代品", expected=True)
            sound = max(audio, key=lambda f: f.get("tbr") or 0) if audio else None
            complete = []
            for fmt in formats:
                if fmt.get("vcodec") == "none":
                    continue
                if sound and not str(fmt.get("vcodec") or "").startswith(("avc1", "h264")):
                    continue
                if duration:
                    fmt["_instagramDuration"] = duration
                fmt["_instagramAudioExpected"] = expected_audio
                if sound:
                    fmt["_instagramAudio"] = {"url": sound["url"], "formatId": sound.get("format_id", "audio"),
                                              "language": sound.get("language") or "und"}
                    fmt["_instagramAudioExpected"] = True
                    fmt["acodec"] = sound["acodec"]
                    fmt["format_id"] = f"{fmt.get('format_id') or 'video'}+{sound.get('format_id') or 'audio'}"
                complete.append(fmt)
            if sound and not complete:
                raise ShortMediaUnsupported("Instagram 清单未提供可合并的 H264 原始视频", expected=True)
            info["formats"] = complete
        if media.get("media_type") == 1:
            renditions = traverse_obj(media, ("image_versions2", "candidates", {list})) or []
            photos = [r for r in renditions[:50] if isinstance(r, dict)]
            photos.sort(key=lambda r: r.get("width") if isinstance(r.get("width"), int) else 0, reverse=True)
            photo = _picture([r.get("url") for r in photos], info.get("id"), "作品图片")
            if photo:
                info.update(photo)
        return info

    def _parse_mpd_formats(self, document, *args, **kwargs):
        # Preflight forbids timeline expansion and external references. Native
        # yt-dlp parses codec/representation metadata; SegmentBase URLs are fetched
        # as complete files rather than interpreting their byte-range indexes.
        document = _instagram_manifest(ET.tostring(document, encoding="unicode"))
        formats = super()._parse_mpd_formats(document, *args, **kwargs)
        if any(f.get("fragments") or f.get("has_drm") or not _instagram_track_url(f.get("url")) for f in formats):
            raise ShortMediaUnsupported("Instagram 清单未提供完整文件音视频轨道", expected=True)
        for fmt in formats:
            fmt["_instagramDash"] = True
        return formats


SHORT_EXTRACTORS = {"tiktok": _ShortTikTokIE, "douyin": _ShortDouyinIE, "instagram": _ShortInstagramIE}


def watermark_evidence(platform, fmt):
    note = str(fmt.get("format_note") or "").lower()
    if ("watermarked" in note or (platform == "douyin"
            and "/playwm/" in urlsplit(fmt.get("url") or "").path.lower())):
        return "embedded"
    if fmt.get("_sourceClean") is True:
        return "source-clean"
    if platform in {"douyin", "tiktok"} and (
        fmt.get("_playback") or str(fmt.get("format_id") or "").startswith("play")
        or "direct video" in note or "playback video" in note
    ):
        return "playback"
    return "unknown"


def short_identity(asset):
    evidence = asset.get("shortMedia") or {}
    if not isinstance(evidence, dict) or not evidence.get("itemId"):
        return None
    target = short_target(asset.get("pageUrl"))
    if (not target or not target.post_id or target.platform != evidence.get("platform")
            or target.post_id != evidence.get("postId") or asset.get("_resolver") != f"yt-dlp:{target.platform}"):
        return None
    if asset.get("kind") == "video" and not asset.get("_formatId"):
        return None
    key = ["short-v1", target.platform, target.post_id, evidence["itemId"], asset["kind"],
           asset.get("engine"), asset.get("_formatId"), evidence.get("watermark")]
    return hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()

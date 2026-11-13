"""Read Chunlou's public episode directory and validate the addressed player's own HLS evidence."""

import asyncio
import json
import math
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

# Published by Chunlou's own application config. A config change must not redirect a bearer token.
AUTH_PROJECT = "grzjuztzhrexhvmetnvd"
AUTH_ISSUER = f"https://{AUTH_PROJECT}.supabase.co/auth/v1"
# The official app initializes auth with supabaseLbUrl, while tokens retain the primary issuer.
AUTH_STORAGE_KEY = f"sb-{AUTH_PROJECT}-all-auth-token"
LEGACY_AUTH_STORAGE_KEY = f"sb-{AUTH_PROJECT}-auth-token"
_ORIGIN = "https://chunlou.tv"
_MEDIA = "https://hls.chunlou.tv"
_UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", re.I)
_SLUG = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}")
_MAIN = re.compile(r"/assets/index-[\w.-]{1,120}\.js")
_CONFIG = re.compile(r"""(?:from|import\()\s*["']\./(config-[\w.-]{1,120}\.js)["']""")
_PROJECT = re.compile(r"https://[a-z0-9]{12,40}\.supabase\.co")
_EP_FIELDS = (
    "id,drama_id,seq,display_no,kind,status,duration_sec,coin_price,preview_sec,preview_locked"
)
_SHOW_FIELDS = "id,title_i18n,slug,status,completed,free_episodes,vip_only,min_level,cover_path,banner_path,poster_path"


class DirectoryAccessRequired(ValueError):
    """The source requires user interaction before its directory can be read."""


def _canonical_url(value):
    try:
        parts = urlsplit(value)
        if (
            parts.scheme == "https"
            and parts.hostname in {"chunlou.tv", "www.chunlou.tv"}
            and parts.port in {None, 443}
            and parts.username is None
            and parts.password is None
        ):
            return parts
    except (TypeError, ValueError):
        pass
    return None


def canonical_source_url(url):
    """The published www alias redirects to the bare HTTPS origin that owns login storage."""
    parsed = _canonical_url(url)
    if parsed and len(url) <= 16384 and not any(ord(char) < 32 for char in url):
        return parsed._replace(netloc="chunlou.tv").geturl()
    return url


def restore_account_key(state, site_url):
    """Upgrade only the working copy passed to Chromium; an existing current account wins."""
    if state and _canonical_url(site_url):
        for origin in state.get("origins", []):
            if origin.get("origin") != _ORIGIN:
                continue
            entries = origin.get("localStorage", [])
            if any(item.get("name") == AUTH_STORAGE_KEY for item in entries):
                continue
            for item in entries:
                if item.get("name") == LEGACY_AUTH_STORAGE_KEY:
                    item["name"] = AUTH_STORAGE_KEY
                    break
    return state


def episode_route(url):
    parsed = _canonical_url(url)
    if not parsed or parsed.path.rstrip("/") != "/feed":
        return None
    values = parse_qs(parsed.query, keep_blank_values=True).get("ep", [])
    if len(values) == 1 and _UUID.fullmatch(values[0]):
        return values[0].lower()
    return None


def drama_route(url):
    parsed = _canonical_url(url)
    if not parsed:
        return None
    if parsed.path.rstrip("/") == "/feed":
        query = parse_qs(parsed.query, keep_blank_values=True)
        values = query.get("d", [])
        if "ep" not in query and len(values) == 1 and _SLUG.fullmatch(values[0]):
            return values[0]
    if match := re.fullmatch(r"/drama/([^/]+)/?", parsed.path):
        if _SLUG.fullmatch(match[1]):
            return match[1]
    return None


class _EntryScript(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        path = attrs.get("src") or ""
        if tag == "script" and attrs.get("type") == "module" and _MAIN.fullmatch(path):
            self.scripts.add(path)


async def _get(client, policy, url, *, headers=None, params=None, count=False):
    await policy.validate(url)
    client.cookies.clear()
    async with client.stream("GET", url, headers=headers, params=params) as res:
        if res.status_code in {401, 402, 403}:
            raise DirectoryAccessRequired("春楼目录需要站点授权，请在来源窗口处理后再试")
        if res.status_code == 429:
            raise ValueError("春楼目录暂时限制访问频率，请稍后重试")
        if res.status_code != 200 and not (count and res.status_code == 206):
            raise ValueError(f"春楼目录请求失败（HTTP {res.status_code}）")
        body = bytearray()
        async for chunk in res.aiter_bytes(65536):
            if len(body) + len(chunk) > 2 * 1024**2:
                raise ValueError("春楼目录响应超过读取上限")
            body.extend(chunk)
        if count:
            span = re.fullmatch(r"(?:0-[0-9]+|\*)/([0-9]+)", res.headers.get("content-range", ""))
            return bytes(body), int(span[1]) if span else None
        return bytes(body)


def _client(policy):
    return httpx.AsyncClient(
        timeout=10,
        follow_redirects=False,
        trust_env=False,
        proxy=getattr(policy, "proxy_url", None),
    )


async def _directory_origin(client, policy):
    html = (await _get(client, policy, _ORIGIN + "/")).decode("utf-8-sig")
    parser = _EntryScript()
    parser.feed(html)
    if len(parser.scripts) != 1:
        raise ValueError("未能确认春楼首页的应用入口")
    bundle = (await _get(client, policy, _ORIGIN + parser.scripts.pop())).decode()
    names = set(_CONFIG.findall(bundle))
    if len(names) != 1:
        raise ValueError("未能确认春楼应用的公开目录配置")
    config = (await _get(client, policy, _ORIGIN + "/assets/" + names.pop())).decode()
    origins = re.findall(r"""\bsupabaseUrl\s*:\s*[`"']([^`"']+)[`"']""", config)
    keys = re.findall(
        r"""\bsupabaseAnonKey\s*:\s*[`"'](sb_publishable_[\w-]{10,256})[`"']""", config
    )
    if len(set(origins)) != 1 or len(set(keys)) != 1 or not _PROJECT.fullmatch(origins[0]):
        raise ValueError("春楼公开目录配置已变更")
    return origins[0] + "/rest/v1/", {
        "apikey": keys[0],
        "Accept": "application/json",
        "Origin": _ORIGIN,
        "Referer": _ORIGIN + "/",
    }


async def verify_account(policy, token, user_id):
    """Confirm the imported access token at its pinned issuer before replacing a saved account."""
    try:
        async with asyncio.timeout(25), _client(policy) as client:
            try:
                origin, headers = await _directory_origin(client, policy)
            except ValueError:
                raise ValueError("无法确认春楼登录服务的公开配置，请稍后重试") from None
            if origin != f"https://{AUTH_PROJECT}.supabase.co/rest/v1/":
                raise ValueError("春楼账号服务配置已变更，请更新拾帧后重试")
            url = AUTH_ISSUER + "/user"
            await policy.validate(url)
            client.cookies.clear()
            headers = {**headers, "Authorization": "Bearer " + token}
            async with client.stream("GET", url, headers=headers) as res:
                if res.status_code in {401, 403}:
                    raise ValueError("春楼身份服务未接受该登录，请刷新已登录的春楼页面后重试")
                if res.status_code != 200:
                    raise ValueError("春楼身份服务暂未返回账号信息，请稍后重试")
                body = bytearray()
                async for chunk in res.aiter_bytes(16384):
                    if len(body) + len(chunk) > 65536:
                        raise ValueError("春楼身份服务的响应过大，账号尚未导入")
                    body.extend(chunk)
        try:
            identity = json.loads(body)
        except (ValueError, RecursionError):
            raise ValueError("春楼身份服务返回的数据无效，请稍后重试") from None
        if (
            not isinstance(identity, dict)
            or identity.get("id") != user_id
            or identity.get("is_anonymous")
        ):
            raise ValueError("春楼身份服务未确认此账号，请返回已登录的春楼页面重新授权")
    except (httpx.HTTPError, TimeoutError, OSError):
        raise ValueError("无法连接春楼身份服务，请检查当前网络后重试") from None


def _episode_info(row, show):
    identifier, seq = row.get("id"), row.get("seq")
    if (
        not isinstance(identifier, str)
        or not _UUID.fullmatch(identifier)
        or type(seq) is not int
        or not 1 <= seq <= 1000
    ):
        raise ValueError("春楼目录包含无效集号或作品编号")
    ordinal = row.get("display_no") if row.get("display_no") is not None else seq
    if type(ordinal) is not int or not 1 <= ordinal <= 1000:
        raise ValueError("春楼目录返回了无效的主线显示集号")
    price = row.get("coin_price")
    free = show.get("free_episodes")
    in_free_range = type(free) is int and seq <= free
    restricted = (
        show.get("vip_only") is True
        or (show.get("min_level") or 0) > 0
        or not (in_free_range or (type(price) is int and price == 0))
    )
    return {
        "id": identifier.lower(),
        "ordinal": ordinal,
        "duration": row.get("duration_sec"),
        "price": price,
        "previewSeconds": row.get("preview_sec"),
        "previewLocked": row.get("preview_locked") is True,
        "restricted": restricted,
    }


def _record(show, rows, target, *, complete=True):
    from backend.site_adapters import build_catalog

    drama_id, slug = show.get("id"), show.get("slug")
    if (
        not isinstance(drama_id, str)
        or not _UUID.fullmatch(drama_id)
        or not isinstance(slug, str)
        or not _SLUG.fullmatch(slug)
        or show.get("status") not in {"published", "archived"}
    ):
        raise ValueError("未能确认该作品所属的春楼剧集")
    if not isinstance(rows, list) or not rows or len(rows) > 1000:
        raise ValueError("春楼单集目录为空或超出读取上限")
    episodes, ordinals, has_branches = {}, set(), False
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("drama_id") != drama_id
            or row.get("status") != "published"
        ):
            raise ValueError("春楼返回了其它剧或未发布的单集")
        if row.get("kind") != "main":
            has_branches = True
            continue
        ep = _episode_info(row, show)
        if ep["id"] in episodes or ep["ordinal"] in ordinals:
            raise ValueError("春楼目录存在重复作品或集号")
        episodes[ep["id"]] = ep
        ordinals.add(ep["ordinal"])
    if not episodes or (target and target not in episodes):
        raise ValueError("目标作品未列入这部剧的已发布主线目录")
    current = episodes[target] if target else min(episodes.values(), key=lambda e: e["ordinal"])
    titles = show.get("title_i18n") or {}
    title = next(
        (
            titles[k].strip()[:200]
            for k in ("zh", "zh-Hant", "en")
            if isinstance(titles, dict) and isinstance(titles.get(k), str) and titles[k].strip()
        ),
        slug,
    )
    detail_url = f"{_ORIGIN}/drama/{slug}"
    info = {"seriesKey": drama_id, "catalogUrl": detail_url, "title": title}
    links = [
        {
            "url": f"{_ORIGIN}/feed?ep={ep['id']}",
            "ordinal": ep["ordinal"],
            "title": f"第 {ep['ordinal']} 集",
            "access": "needs-user" if ep["restricted"] else "unknown",
        }
        for ep in episodes.values()
    ]
    count = (
        len(ordinals)
        if complete
        and show.get("completed") is True
        and ordinals == set(range(1, len(ordinals) + 1))
        else None
    )
    catalog = build_catalog(detail_url, links, series_metadata=info, expected_count=count)
    for entry in catalog["entries"]:
        if entry["access"] == "needs-user":
            entry["accessEvidence"] = "public-directory"
    catalog["adapterId"] = "chunlou"
    catalog["publishedCount"] = len(episodes)
    catalog["ongoing"] = show.get("completed") is False
    catalog["discoverySources"] = ["site-directory"]
    if not complete:
        catalog["truncated"] = True
        catalog["reasons"] = [*catalog.get("reasons", []), "catalog-limit"]
    if has_branches:
        catalog["reasons"] = [*catalog.get("reasons", []), "main-story-only"]
    images = []
    for field, label in (("cover_path", "封面"), ("banner_path", "横幅"), ("poster_path", "海报")):
        path = show.get(field)
        if (
            isinstance(path, str)
            and len(path) < 1000
            and re.fullmatch(r"[\w./-]+\.(?:webp|jpe?g|png|avif)", path, re.I)
            and all(part not in {"", ".", ".."} for part in path.split("/"))
        ):
            url = f"{_MEDIA}/cover/{path}"
            if not any(image["url"] == url for image in images):
                images.append({"url": url, "title": f"{title} · {label}"})
    if images:
        catalog["coverUrls"] = [image["url"] for image in images]
    return {
        "id": current["id"],
        "episode": current,
        "episodes": episodes,
        "dramaId": drama_id,
        "title": title,
        "catalog": catalog,
        "images": images,
        "mediaUrl": f"{_MEDIA}/hls/{current['id']}/master.m3u8",
    }


async def _read(policy, target, slug):

    async def rows(table, fields, **filters):
        raw = await _get(
            client, policy, origin + table, headers=headers, params={"select": fields, **filters}
        )
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("春楼目录返回了无效数据")
        return data

    try:
        async with (
            asyncio.timeout(35),
            _client(policy) as client,
        ):
            origin, headers = await _directory_origin(client, policy)
            if target:
                found = await rows(
                    "episodes",
                    f"{_EP_FIELDS},drama:dramas!inner({_SHOW_FIELDS})",
                    id="eq." + target,
                    status="eq.published",
                    limit="2",
                )
                if (
                    len(found) != 1
                    or not isinstance(found[0], dict)
                    or found[0].get("id") != target
                    or not isinstance(found[0].get("drama"), dict)
                ):
                    raise ValueError("没有找到与链接编号相符的已发布春楼单集")
                show = found[0]["drama"]
                if found[0].get("drama_id") != show.get("id"):
                    raise ValueError("春楼单集和所属剧集的编号不匹配")
            else:
                found = await rows(
                    "dramas",
                    _SHOW_FIELDS,
                    slug="eq." + slug,
                    status="in.(published,archived)",
                    limit="2",
                )
                if (
                    len(found) != 1
                    or not isinstance(found[0], dict)
                    or found[0].get("slug") != slug
                ):
                    raise ValueError("未找到与该春楼详情链接对应的剧集")
                show = found[0]
            identifier = show.get("id")
            if not isinstance(identifier, str) or not _UUID.fullmatch(identifier):
                raise ValueError("春楼目录返回了无效剧集编号")
            raw, total = await _get(
                client,
                policy,
                origin + "episodes",
                headers={**headers, "Prefer": "count=exact"},
                params={
                    "select": _EP_FIELDS,
                    "drama_id": "eq." + identifier,
                    "status": "eq.published",
                    "order": "seq.asc",
                    "limit": "1000",
                },
                count=True,
            )
            listing = json.loads(raw)
            return _record(
                show, listing, target, complete=isinstance(listing, list) and total == len(listing)
            )
    except (httpx.HTTPError, TimeoutError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        raise ValueError("春楼公开目录暂不可用或数据格式已变更，请稍后重试") from None


async def read_episode(url, policy):
    target = episode_route(url)
    if not target:
        raise ValueError("请提供包含明确作品编号的春楼播放链接")
    return await _read(policy, target, None)


async def read_drama(url, policy):
    slug = drama_route(url)
    if not slug:
        raise ValueError("请提供春楼剧集详情链接")
    return await _read(policy, None, slug)


def _media_path(url, identifier):
    if not isinstance(url, str) or not isinstance(identifier, str):
        return ""
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme == "https"
            and parsed.netloc == "hls.chunlou.tv"
            and not parsed.fragment
            and _UUID.fullmatch(identifier)
            and parsed.path.startswith(f"/hls/{identifier}/")
            and all(
                re.fullmatch(r"[A-Za-z0-9_.-]+", part) and part not in {".", ".."}
                for part in parsed.path.strip("/").split("/")
            )
        ):
            return parsed.path.removeprefix(f"/hls/{identifier}/")
    except (ValueError, TypeError):
        pass
    return ""


def master_url_matches(url, identifier):
    return _media_path(url, identifier) == "master.m3u8"


def manifest_url_matches(url, identifier):
    path = _media_path(url, identifier)
    return path == "master.m3u8" or bool(re.fullmatch(r"(?:[\w-]+/){1,4}index\.m3u8", path))


def _attributes(line):
    """Reject ambiguous/malformed attributes, including duplicate key URIs."""
    value = line.partition(":")[2]
    pattern = r'([A-Z0-9-]+)=(?:"([^"\r\n]*)"|([^,\r\n"]+))(,|$)'
    result = {}
    offset = 0
    for match in re.finditer(pattern, value):
        if match.start() != offset or match[1] in result:
            return {}
        result[match[1]] = match[2] if match[2] is not None else match[3]
        offset = match.end()
    return result if offset == len(value) else {}


def _joined(base, relative):
    try:
        return urljoin(base, relative)
    except (TypeError, ValueError):
        return ""


def chunlou_key_url(uri, identifier):
    """Resolve the player's key alias without decoding or re-encoding its signed query."""
    if (
        not isinstance(identifier, str)
        or not _UUID.fullmatch(identifier)
        or not isinstance(uri, str)
        or not uri.startswith("chunlou://hls.chunlou.tv/")
        or len(uri) > 16384
        or any(ord(c) < 33 or ord(c) == 127 or c in "\\#" for c in uri)
    ):
        return None
    parts = urlsplit(uri)
    if parts.netloc != "hls.chunlou.tv" or not re.fullmatch(
        rf"/key/{re.escape(identifier)}/[0-9]+", parts.path
    ):
        return None
    return "https://" + uri.removeprefix("chunlou://")


def _key_supported(attrs, base, identifier):
    if attrs.get("METHOD") == "NONE":
        return set(attrs) == {"METHOD"}
    uri, iv = attrs.get("URI", ""), attrs.get("IV", "")
    if (
        attrs.get("METHOD") != "AES-128"
        or not uri
        or attrs.get("KEYFORMAT", "identity") != "identity"
        or attrs.get("KEYFORMATVERSIONS", "1") != "1"
        or (iv and not re.fullmatch(r"0x[0-9a-fA-F]{1,32}", iv))
    ):
        return False
    if uri.startswith("chunlou:"):
        return chunlou_key_url(uri, identifier) is not None
    target = _joined(base, uri)
    try:
        key = urlsplit(target)
    except ValueError:
        return False
    return (
        key.scheme == "https"
        and key.netloc == "hls.chunlou.tv"
        and not key.fragment
        and (
            bool(_media_path(target, identifier))
            or re.fullmatch(rf"/key/{re.escape(identifier)}/[0-9]+", key.path) is not None
        )
    )


def _rendition(lines, url, identifier):
    """Return the VOD duration only after validating all resources the downloader will read."""
    if not lines or lines[0] != "#EXTM3U":
        return "invalid", 0
    protected = any(
        line.startswith("#EXT-X-SESSION-KEY:")
        or (
            line.startswith("#EXT-X-KEY:")
            and not _key_supported(_attributes(line), url, identifier)
        )
        for line in lines
    )
    state, total = vod_duration(lines, url, identifier)
    if protected:
        # Only a complete, validated playlist can establish that the protected clip is a preview.
        return "protected", total if state == "full" else 0
    return state, total


def vod_duration(lines, url, identifier):
    total = 0.0
    if lines[-1] != "#EXT-X-ENDLIST" or lines.count("#EXT-X-ENDLIST") != 1:
        return "invalid", total
    pending, key = None, {}
    for line in lines:
        if pending is not None:
            if line.startswith("#") or not _media_path(_joined(url, line), identifier):
                return "invalid", 0
            total += pending
            pending = None
        elif line.startswith("#EXTINF:"):
            try:
                pending = float(line[8:].partition(",")[0])
            except (ValueError, OverflowError):
                return "invalid", 0
            if not math.isfinite(pending) or pending <= 0:
                return "invalid", 0
        elif not line.startswith("#"):
            return "invalid", 0
        elif line.startswith("#EXT-X-KEY:"):
            key = _attributes(line)
        elif line.startswith("#EXT-X-MAP:"):
            attrs = _attributes(line)
            if (
                not attrs.get("URI")
                or "BYTERANGE" in attrs
                or (key.get("METHOD") == "AES-128" and not key.get("IV"))
                or not _media_path(_joined(url, attrs["URI"]), identifier)
            ):
                return "extension", 0
        elif (
            line.startswith(
                (
                    "#EXT-X-BYTERANGE:",
                    "#EXT-X-DEFINE:",
                    "#EXT-X-PART:",
                    "#EXT-X-GAP",
                    "#EXT-X-STREAM-INF:",
                    "#EXT-X-MEDIA:",
                )
            )
            or "URI=" in line
        ):
            return "extension", 0
    return "full", total


def _matches_short_preview(episode, total):
    duration, preview = episode.get("duration"), episode.get("previewSeconds")
    return (
        episode.get("restricted")
        and type(duration) in (int, float)
        and type(preview) in (int, float)
        and 0 < preview < duration
        and total > 0
        and duration - total > 1.25
        and abs(total - preview) <= 1.25
    )


def inspect_playback(record, manifests):
    """Check 200-response bodies from the exact page, after its user-facing gates cleared.

    Return only an actually observed, complete VOD rendition. The caller must keep
    signed transport URLs private. A public episode listing alone never supplies one.
    """
    identifier = record["id"]
    masters = [
        (url, body)
        for url, body in manifests.items()
        if master_url_matches(url, identifier)
        and isinstance(body, str)
        and len(body) <= 2 * 1024**2
        and body.lstrip().startswith("#EXTM3U")
    ]
    choices = {}
    unsupported = extension = preview = False
    for master, text in masters:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines or lines[0] != "#EXTM3U":
            continue
        external_audio = {
            attrs.get("GROUP-ID")
            for row in lines
            if row.startswith("#EXT-X-MEDIA:")
            if (attrs := _attributes(row)).get("TYPE") == "AUDIO" and attrs.get("URI")
        }
        master_protected = any(line.startswith("#EXT-X-SESSION-KEY:") for line in lines)
        saw_variant = False
        for index, line in enumerate(lines[:-1]):
            if not line.startswith("#EXT-X-STREAM-INF:"):
                continue
            url = _joined(master, lines[index + 1])
            if not manifest_url_matches(url, identifier) or master_url_matches(url, identifier):
                continue
            body = manifests.get(url)
            if (
                not isinstance(body, str)
                or len(body) > 2 * 1024**2
                or not body.lstrip().startswith("#EXTM3U")
            ):
                continue
            contents = [row.strip() for row in body.splitlines() if row.strip()]
            state, total = _rendition(contents, url, identifier)
            saw_variant = True
            if state == "protected" or master_protected:
                if state in {"protected", "full"} and _matches_short_preview(
                    record["episode"], total
                ):
                    preview = True
                else:
                    unsupported = True
                continue
            stream = _attributes(line)
            selected_audio = stream.get("AUDIO")
            if state == "extension" or (selected_audio and selected_audio in external_audio):
                extension = True
                continue
            if state != "full" or total <= 0 or not stream:
                continue
            episode = record["episode"]
            target = episode["duration"]
            if type(target) not in (float, int) or not math.isfinite(target) or target <= 0:
                continue
            preview_length = episode.get("previewSeconds")
            if (
                episode.get("restricted")
                and type(preview_length) in (int, float)
                and 0 < preview_length < target
                and total <= preview_length + 0.5
            ):
                preview = True
                continue
            if abs(total - target) > 1.25:
                preview = preview or total < target
                continue
            quality = re.fullmatch(r"\d+x(\d{1,5})", stream.get("RESOLUTION", ""))
            custom_aes = any(
                row.startswith("#EXT-X-KEY:") and _attributes(row).get("URI", "").startswith("chunlou:")
                for row in contents
            )
            choices[url] = (int(quality[1]) if quality else 0, total, custom_aes)
        if master_protected and not saw_variant:
            unsupported = True
    if choices:
        url = max(choices, key=lambda value: choices[value][0])
        return {
            "status": "ready",
            "url": url,
            "duration": choices[url][1],
            "chunlouAes128": choices[url][2],
            "message": "已核对本集播放器的完整点播清单",
        }
    if unsupported:
        return {
            "status": "unsupported",
            "message": "该集使用春楼播放器的专用密钥或保护协议，当前无法导出视频",
        }
    if extension:
        return {
            "status": "unsupported",
            "message": "该播放版本含独立音轨或当前不支持的清单扩展，无法完整导出",
        }
    if preview:
        return {
            "status": "preview",
            "message": "只观察到比本集标注时长更短的播放清单，请检查访问权限",
        }
    return {"status": "unobserved", "message": "已识别这集，尚未观察到可验证的完整播放清单"}

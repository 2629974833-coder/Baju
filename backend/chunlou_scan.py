"""Observe the selected Chunlou player without admitting feed recommendations."""

import asyncio
import base64
import json
import re
from collections import Counter
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Error

from backend.catalog_discovery import same_page
from backend.chunlou import (
    AUTH_PROJECT,
    DirectoryAccessRequired,
    canonical_source_url,
    episode_route,
    inspect_playback,
    manifest_url_matches,
    read_drama,
    read_episode,
)
from backend.session_refresh import ChunlouRelinkRequired

_MAX_PLAYLIST_BYTES = 2 * 1024 * 1024
_AGE_MESSAGE = "春楼要求年龄确认。请在站点窗口自行完成确认并保存会话，然后重新拾取这一集。"
_LOGIN_MESSAGE = (
    "春楼要求登录或 Cloudflare 验证。请在站点授权里打开本机浏览器，登录后保存会话并重试。"
)

_PAGE_STATE = r"""episode => {
  const visible = e => e && e.getClientRects().length > 0;
  const dialogs = [...document.querySelectorAll('[role="alertdialog"],[role="dialog"],[aria-modal="true"]')].filter(visible);
  if (dialogs.some(e => /age verification/i.test(e.getAttribute('aria-label') || '') ||
       /age verification|年龄验证|年齡驗證|18\s*\+/.test(e.innerText.toLowerCase()))) return 'age';
  if ([...document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]')].some(visible)
      || /checking your browser|verify you are human|just a moment/i.test(document.title)) return 'challenge';
  if (location.pathname === '/auth' || [...document.querySelectorAll('input[type=password]')].some(visible)) return 'login';
  if (dialogs.length) return 'dialog';
  const root=document.querySelector('#app');
  // Source Feed slides identify the actual episode. Adjacent slides can preload their own paywalls.
  if (episode && root) {
    const targets = [...root.querySelectorAll('[data-ep][data-idx][data-drama]')]
      .filter(slide => slide.dataset.ep.toLowerCase() === episode);
    const slide = targets.length === 1 ? targets[0] : null;
    const rect = slide?.getBoundingClientRect();
    if (rect && rect.top <= innerHeight / 2 && rect.bottom > innerHeight / 2 &&
        rect.left < innerWidth && rect.right > 0) {
      // These pairs appear only after the official lock overlay has resolved the account.
      const labels = [
        ['Unlock Now','What happens next?','Free preview ended'], ['Get VIP','VIP Exclusive','Preview ended'],
        ['立即解锁','接下来会发生什么？','免费试看结束'], ['开通会员','VIP 专属内容','精彩正片待续'],
        ['立即解鎖','接下來會發生什麼？','免費試看結束'], ['開通會員','VIP 專屬內容','精彩正片待續'],
        ['今すぐ解放','次に何が起こる？','無料視聴終了'], ['VIPに加入','VIP限定コンテンツ','プレビュー終了'],
        ['지금 잠금해제','다음에 무슨 일이?','무료 미리보기 종료'], ['VIP 가입','VIP 전용 콘텐츠','미리보기 종료'],
        ['Desbloquear','¿Qué pasa después?','Fin de la vista previa gratis'],
        ['Hazte VIP','Contenido exclusivo VIP','Vista previa finalizada'],
        ['Débloquer','Que se passe-t-il ensuite ?', "Fin de l'aperçu gratuit"],
        ['Devenir VIP','Contenu VIP exclusif','Aperçu terminé'],
      ];
      for (const heading of slide.querySelectorAll('div.absolute.inset-0 > div > b.font-display')) {
        const overlay = heading.parentElement.parentElement;
        const pair = labels.find(([, ...titles]) => titles.includes(heading.innerText.trim()));
        if (!pair || !visible(heading) || getComputedStyle(heading).visibility !== 'visible') continue;
        const confirmed = [...overlay.querySelectorAll(':scope > button.btn-primary,:scope > button.btn-gold')].some(
          button => visible(button) && !button.disabled && getComputedStyle(button).visibility === 'visible' &&
            button.innerText.split('·')[0].trim() === pair[0]);
        if (confirmed) return 'locked-episode';
      }
      return 'target-slide';
    }
  }
  if (episode && root?.querySelector('main,[role=main]')) return 'shell';
  return root && root.querySelector('main,video,[role=main]') && root.innerText.trim().length ? 'ready' : 'waiting';
}"""


_UNLOCK_HOSTS = {f"{AUTH_PROJECT}.supabase.co", f"{AUTH_PROJECT}-all.supabase.co"}
_EPISODE_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)


def unlock_listing(url):
    """Match only the read-only episode-ID listing the official Chunlou client requests."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc not in _UNLOCK_HOSTS
        or parsed.path != "/rest/v1/unlocks"
    ):
        return False
    fields = parse_qs(parsed.query)
    if fields.get("select") != ["episode_id"] or len(fields.get("user_id", [])) != 1:
        return False
    account = fields["user_id"][0]
    return account.startswith("eq.") and bool(_EPISODE_UUID.fullmatch(account[3:]))


class PlaylistObserver:
    """Read the player's original responses with a decoded-byte budget per playlist."""

    def __init__(self, session, identifier):
        self.session = session
        self.completed = {}
        self.identifier = identifier
        self.unlock_requests = []
        self.denied = False

    @classmethod
    async def attach(cls, page, url, identifier):
        session = await page.context.new_cdp_session(page)
        observer = cls(session, identifier)
        frame_id = (await session.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
        requests = {}

        def response(event):
            network = event["response"]
            if event.get("frameId") != frame_id or not same_page(page.url, url):
                return
            manifest = manifest_url_matches(network["url"], identifier)
            if manifest and network["status"] in {401, 402, 403}:
                observer.denied = True
            is_receipt = event.get("type") in {"XHR", "Fetch"} and unlock_listing(network["url"])
            if len(requests) < 24 and (manifest or is_receipt) and 200 <= network["status"] < 300:
                requests[event["requestId"]] = {
                    "url": network["url"] if manifest else None,
                    "decoded_size": 0,
                }

        def received(event):
            if item := requests.get(event["requestId"]):
                item["decoded_size"] += event[
                    "dataLength"
                ]  # Decoded bytes, even for Brotli/chunked HTTP.

        def finished(event):
            request_id = event["requestId"]
            if (item := requests.get(request_id)) and 0 < item[
                "decoded_size"
            ] <= _MAX_PLAYLIST_BYTES:
                if item["url"] is not None:
                    observer.completed[item["url"]] = request_id
                else:
                    observer.unlock_requests.append(request_id)

        session.on("Network.responseReceived", response)
        session.on("Network.dataReceived", received)
        session.on("Network.loadingFinished", finished)
        await session.send(
            "Network.enable",
            {
                "maxResourceBufferSize": _MAX_PLAYLIST_BYTES,
                "maxTotalBufferSize": 12 * _MAX_PLAYLIST_BYTES,
            },
        )
        return observer

    async def _body(self, request_id):
        try:
            async with asyncio.timeout(3):
                payload = await self.session.send(
                    "Network.getResponseBody", {"requestId": request_id}
                )
            body = payload["body"]
            if payload["base64Encoded"]:
                return base64.b64decode(body, validate=True).decode("utf-8-sig")
            return body.removeprefix("\ufeff")
        except (Error, TimeoutError, ValueError):
            return ""

    async def manifests(self):
        found = {}
        for url, request_id in list(self.completed.items()):
            body = await self._body(request_id)
            if len(body) <= _MAX_PLAYLIST_BYTES and body.lstrip().startswith("#EXTM3U"):
                found[url] = body
        return found

    async def unlocked_episodes(self):
        unlocked = set()
        for request_id in list(self.unlock_requests):
            try:
                rows = json.loads(await self._body(request_id))
            except (ValueError, RecursionError):
                continue
            if isinstance(rows, list) and len(rows) <= 1000:
                for row in rows:
                    episode = row.get("episode_id") if isinstance(row, dict) else None
                    if isinstance(episode, str) and _EPISODE_UUID.fullmatch(episode):
                        unlocked.add(episode.lower())
        return unlocked


async def probe_page(page, url, observer, user_agent, cancelled):
    async def gate():
        if cancelled():
            return {"status": "cancelled", "message": "扫描已取消"}
        if not same_page(page.url, url):
            return {
                "status": "needs-user",
                "message": "春楼跳转到了其他页面，请在站点窗口确认后重试",
            }
        state = await page.evaluate(_PAGE_STATE, episode_route(url))
        if state in {"age", "login", "dialog", "challenge"}:
            message = {
                "age": _AGE_MESSAGE,
                "challenge": "春楼要求通过 Cloudflare 人机验证，请在本机浏览器完成后保存会话。",
                "login": _LOGIN_MESSAGE,
                "dialog": "春楼要求在站点窗口处理弹窗，请完成后保存会话",
            }[state]
            return {"status": "needs-user", "message": message}
        if state in {"ready", "shell", "target-slide", "locked-episode"}:
            return {
                "status": "completed",
                "message": "已读取春楼作品页",
                "_episodeLocked": state == "locked-episode",
                "_targetSlide": state in {"target-slide", "locked-episode"},
            }
        return None

    # The Vue shell may render before the gate or player. Wait for the actual application.
    ready_polls = 0
    for _ in range(24):
        state = await gate()
        if state and state["status"] != "completed":
            return state
        if state:
            ready_polls += 1
            media_ready = (
                not observer
                or not observer.identifier
                or len(observer.completed) >= 2
                or observer.denied
            )
            if (
                media_ready
                and not state["_episodeLocked"]
                and (not observer or observer.unlock_requests or ready_polls >= 4)
            ):
                break
        await page.wait_for_timeout(250)
    state = await gate()
    if not state:
        return {"status": "failed", "message": "春楼页面未加载到可识别的状态，请稍后重试"}
    if state["status"] != "completed":
        return state
    manifests = await observer.manifests() if observer else {}
    agents = {url: user_agent for url in manifests if user_agent and len(user_agent) < 1024}
    final = await gate()
    if not final or final["status"] != "completed":
        return final or {"status": "failed", "message": "春楼作品页已发生变化"}
    if episode_route(url) and not (final["_targetSlide"] or manifests or (observer and observer.denied)):
        return {
            "status": "failed",
            "message": "春楼页面已打开，目标集的播放器尚未出现，也未收到该集的播放清单。请重试，若持续出现请到来源检查会话。",
            "_canReadDirectory": True,
            "_unlocked": await observer.unlocked_episodes() if observer else set(),
        }
    return {
        **final,
        "_manifests": manifests,
        "_agents": agents,
        "_unlocked": await observer.unlocked_episodes() if observer else set(),
        "_manifestDenied": bool(observer and observer.denied),
    }


def _series_summary(record, outcomes, *, selected_only=False):
    entries = record["catalog"]["entries"]
    statuses = Counter(outcome["status"] for outcome in outcomes.values())
    summary = f"已列出《{record['title']}》{len(entries)} 集，本次检查 {len(outcomes)} 集。"
    labels = []
    if count := statuses["ready"]:
        labels.append(f"可下载 {count} 集")
    if count := statuses["unsupported-media"] + statuses["unsupported-drm"]:
        labels.append(f"暂不支持导出 {count} 集")
    if count := statuses["needs-user"]:
        labels.append(f"站点要求处理权限或验证 {count} 集（处理后可复查）")
    if count := statuses["failed"] + statuses["timed-out"]:
        labels.append(f"未能解析 {count} 集（可重扫）")
    if labels:
        summary += "，".join(labels) + "。"
    pending = sum(
        entry.get("accessEvidence") == "public-directory"
        and (not selected_only or entry["pageUrl"] in outcomes)
        for entry in entries
    )
    if pending:
        scope = "本次检查中" if selected_only else "目录中"
        summary += f"{scope} {pending} 集的账号权限待核实。"
    return summary


async def scan_chunlou(scanner, site, options, on_asset, on_progress, cancelled):
    start = canonical_source_url(options["url"])
    series = options.get("mode") == "series"
    kinds = options.get("kinds") or ["image", "video"]
    budget = min(1000, max(1, int(options.get("limit", 1)))) if series else 1
    pages, found = 0, 0
    record = None
    access = {}
    outcomes = {}
    recheck_targets = {canonical_source_url(url) for url in options.get("recheckUrls", [])}
    recheck_attempts = []
    limited = False
    title = ""

    async def report(status, message):
        result = {
            "status": status,
            "message": message,
            "pagesVisited": pages,
            "assetsFound": found,
            "limited": limited,
            "_pageAccess": dict(access),
            "_episodeOutcomes": dict(outcomes),
            "_recheckAttemptedUrls": list(recheck_attempts),
        }
        if record:
            if series:
                result["catalog"] = record["catalog"]
            result["workTitle"] = title or record["title"]
        await on_progress(result)
        return result

    async def probe(url):
        nonlocal pages

        async def ignore(_item):
            pass

        result = await scanner.scan(
            site,
            {
                "url": url,
                "mode": "page",
                "depth": 0,
                "limit": 1,
                "_chunlouProbe": True,
                "readOnly": True,
            },
            ignore,
            ignore,
            cancelled,
        )
        pages += 1
        if url in recheck_targets and url not in recheck_attempts:
            recheck_attempts.append(url)
        return result

    def remember(url, status, message, entry=None):
        identifier = episode_route(url)
        if identifier and status != "cancelled":
            url = f"https://chunlou.tv/feed?ep={identifier}"
            outcomes[url] = {"status": status, "message": message}
            if status == "needs-user":
                access[url] = "needs-user"
                if entry is not None:
                    entry.update(access="needs-user", accessEvidence="player")

    seeds = list(dict.fromkeys(canonical_source_url(url) for url in options.get("seedUrls", [])))
    # A gate on the first selected episode must not prevent checking the rest of a batch.
    initial_targets = [start] + [url for url in seeds if url != start and episode_route(url)]
    visited_before_directory = set()
    previous_catalog = options.get("catalog") or {}
    last_status, last_message = "failed", "未能读取春楼作品目录"
    first = None
    catalog_source = start
    await report("running", "正在读取春楼作品目录")
    for candidate in initial_targets[:budget]:
        if cancelled():
            return await report("cancelled", "扫描已取消")
        try:
            first = await probe(candidate)
        except ChunlouRelinkRequired as error:
            return {**await report("needs-user", str(error)), "_stopBatch": True}
        last_status, last_message = first["status"], first["message"]
        if last_status == "cancelled":
            return await report(last_status, last_message)
        if last_status == "completed" or first.get("_canReadDirectory"):
            try:
                record = await (
                    read_episode(candidate, scanner.policy)
                    if episode_route(candidate)
                    else read_drama(candidate, scanner.policy)
                )
            except ValueError as error:
                last_status = (
                    "needs-user" if isinstance(error, DirectoryAccessRequired) else "failed"
                )
                last_message = str(error)
            else:
                if (
                    previous_catalog.get("adapterId") == "chunlou"
                    and previous_catalog.get("seriesKey") != record["dramaId"]
                ):
                    record = None
                    return await report("failed", "春楼作品所属剧发生变化，请重新拾取")
                catalog_source = candidate
                break
        remember(candidate, last_status, last_message)
        visited_before_directory.add(candidate)
        if len(initial_targets) > 1:
            await report("running", f"已检查 {len(outcomes)} 集，正在继续读取目录")
    if record is None:
        limited = pages < len(initial_targets)
        return await report(last_status, last_message)
    catalog = record["catalog"]
    entries = {entry["pageUrl"]: entry for entry in catalog["entries"]}
    for page_url, result in outcomes.items():
        if result["status"] == "needs-user" and page_url in entries:
            entries[page_url].update(access="needs-user", accessEvidence="player")

    def apply_unlocks(seen):
        for identifier in seen.get("_unlocked", ()):
            page_url = f"https://chunlou.tv/feed?ep={identifier}"
            row = entries.get(page_url)
            if row and row.get("accessEvidence") == "public-directory" and page_url not in outcomes:
                row.update(access="observed", accessEvidence="account-unlock")

    apply_unlocks(first)
    excluded = set(options.get("excludedEpisodeKeys", []))
    if not series and episode_route(start):
        title = f"{record['title']} · 第 {record['episode']['ordinal']} 集"
    if "image" in kinds:
        for image in record["images"]:
            if cancelled():
                return await report("cancelled", "扫描已取消")
            await scanner.policy.validate(image["url"])
            await on_asset(
                {
                    "kind": "image",
                    "engine": "http",
                    "sourceUrl": image["url"],
                    "pageUrl": catalog["catalogUrl"],
                    "_intakePageUrl": catalog["catalogUrl"] if series else start,
                    "title": image["title"],
                    "thumbnail": image["url"],
                    "_readOnly": True,
                    "category": "图片素材",
                    "_headers": {"Referer": start},
                }
            )
            found += 1
    if not series:
        targets = [start]
    elif seeds:
        targets = seeds
    else:
        targets = [e["pageUrl"] for e in catalog["entries"] if e["access"] != "needs-user"]
        if start_id := episode_route(start):
            targets = [start, *(url for url in targets if episode_route(url) != start_id)]
    if "video" not in kinds:
        targets = []
    for target in targets:
        identifier = episode_route(target)
        canonical = f"https://chunlou.tv/feed?ep={identifier}"
        entry = entries.get(canonical)
        if target in visited_before_directory or not entry or entry["episodeKey"] in excluded:
            continue
        if cancelled():
            return await report("cancelled", "扫描已取消")
        if target != catalog_source and pages >= budget:
            limited = True
            break
        await report("running", f"正在扫描第 {entry['ordinal']} 集，已检查 {len(outcomes)} 集")
        try:
            seen = first if target == catalog_source else await probe(target)
        except ChunlouRelinkRequired as error:
            return {**await report("needs-user", str(error)), "_stopBatch": True}
        if seen["status"] != "completed":
            remember(canonical, seen["status"], seen["message"], entry)
            if seen["status"] == "cancelled" or not series:
                return await report(seen["status"], seen["message"])
            await report("running", f"已检查第 {entry['ordinal']} 集：{seen['message']}")
            continue
        apply_unlocks(seen)
        item = {**record, "id": identifier, "episode": record["episodes"][identifier]}
        if seen.get("_episodeLocked"):
            outcome = {"status": "locked", "message": "本集播放器显示需要解锁或开通会员"}
        else:
            outcome = inspect_playback(item, seen.get("_manifests", {}))
        label = f"{item['title']} · 第 {item['episode']['ordinal']} 集"
        if (
            outcome.get("chunlouAes128")
            and item["episode"]["restricted"]
            and entry.get("accessEvidence") != "account-unlock"
        ):
            # The feed preloads complete manifests even for adjacent, unpurchased episodes.
            message = f"{label}：已读取播放清单，当前账号对本集的观看权限尚未核实，请在站点确认后复查"
            remember(canonical, "access-pending", message)
            if not series:
                return await report("needs-user", message)
            await report("running", f"已检查第 {entry['ordinal']} 集：账号权限待核实")
            continue
        if outcome["status"] == "ready":
            headers = {"Referer": target, "Origin": f"https://{urlsplit(target).netloc}"}
            ua = seen.get("_agents", {}).get(outcome["url"])
            if ua:
                headers["User-Agent"] = ua
            await on_asset(
                {
                    "kind": "video",
                    "engine": "yt-dlp",
                    "title": label,
                    "sourceUrl": outcome["url"],
                    "pageUrl": canonical,
                    "duration": outcome["duration"],
                    **({"_chunlouDuration": outcome["duration"]} if outcome["chunlouAes128"] else {}),
                    "_chunlouEpisode": identifier,
                    "_readOnly": True,
                    "_mediaLabel": "正片",
                    "category": "视频素材",
                    "_headers": headers,
                }
            )
            found += 1
            access[canonical] = "observed"
            entry["access"] = "observed"
            if entry.get("accessEvidence") != "account-unlock":
                entry["accessEvidence"] = "player"
            remember(canonical, "ready", outcome["message"])
        else:
            state = "unsupported-media" if outcome["status"] == "unsupported" else "failed"
            if outcome["status"] in {"preview", "locked"} or (
                state == "failed" and seen.get("_manifestDenied")
            ):
                state = "needs-user"
                if seen.get("_manifestDenied"):
                    outcome["message"] = "站点拒绝了本集播放器的播放清单，请检查账号权限"
            if state == "unsupported-media" and not item["episode"]["restricted"]:
                access[canonical] = "observed"
                entry.update(access="observed", accessEvidence="player")
            message = f"{label}：{outcome['message']}"
            remember(canonical, state, message, entry)
            if not series:
                return await report(state, message)
        if series:
            await report("running", f"已检查第 {entry['ordinal']} 集：{outcome['message']}")
    if series:
        summary = _series_summary(record, outcomes, selected_only=bool(seeds))
    else:
        summary = f"已读取{title or record['title']}，发现 {found} 项可保存素材"
    return await report("completed", summary)

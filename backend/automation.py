"""Periodic single-page checks. Rule checkpoints and queue admission share one transaction."""

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from backend.classification import classify_asset
from backend.operations import StoragePressure
from backend.series import page_origin, series_identifier
from backend.site_adapters import select_episode_asset
from backend.store import record, timestamp
from backend.tasks import Conflict, find

logger = logging.getLogger(__name__)


class RuleNotFound(KeyError):
    """The requested automation rule no longer exists."""


def public_rule(rule):
    return {key: value for key, value in rule.items() if not key.startswith("_")}


def next_check(rule):
    if not rule["enabled"]:
        return None
    return (datetime.now(UTC) + timedelta(minutes=rule["intervalMinutes"])).isoformat()


def _hidden_episode_pages(state, site_id):
    hidden = set(state.get("_dismissedSeries", []))
    omitted = state.get("_dismissedEpisodes", {})
    pages = set()
    if hidden or omitted:
        for prior in state["scans"]:
            if prior.get("siteId") != site_id:
                continue
            series = series_identifier(prior)
            if series not in hidden and series not in omitted:
                continue
            for entry in (prior.get("catalog") or {}).get("entries", []):
                if series in hidden or entry["episodeKey"] in omitted[series]:
                    pages.add(entry["pageUrl"])
    return pages


def _trim_page_history(scans, rule_id):
    retained = []
    page_checks = 0
    for scan in scans:  # Newest first.
        catalog = scan.get("catalog") or {}
        if (scan.get("ruleId") == rule_id and not catalog.get("entries")
                and not catalog.get("expectedCount")):
            page_checks += 1
            if page_checks > 30:
                continue
        retained.append(scan)
    scans[:] = retained


def _downloadable_ids(state, rule, scan, observed, fresh):
    # Use all observed candidates (including existing variants) when resolving a
    # page's primary video. Only freshly inserted, eligible assets can enter the queue.
    visible = [
        item for item in state["assets"] if item["id"] in observed
        and item["siteId"] == rule["siteId"] and item["kind"] in rule["kinds"]
        and not any(item.get(key) for key in ("locked", "unsupported", "dismissedFromDiscovery"))
    ]
    identifier = series_identifier(scan)
    if identifier in state.get("_dismissedSeries", []):
        return []
    entries = {entry["pageUrl"]: entry for entry in (scan.get("catalog") or {}).get("entries", [])}
    known_gates = _hidden_episode_pages(state, rule["siteId"])
    known_gates.update(url for url, entry in entries.items() if entry.get("access") == "needs-user")
    selected = []
    videos = {}
    for item in visible:
        page = item.get("pageUrl")
        if not page or page in known_gates:
            continue
        if item["kind"] == "image" and item["id"] in fresh:
            selected.append(item["id"])
        elif item["kind"] == "video" and classify_asset(item)["kind"] == "episode":
            videos.setdefault(page, []).append(item)
    for page, candidates in videos.items():
        entry = entries.get(page, {"pageUrl": page, "episodeKey": page})
        choice = select_episode_asset(entry, candidates)["assetId"]
        if choice in fresh:
            selected.append(choice)
    return selected


class AutomationService:
    heartbeat_interval = 5

    def __init__(self, store, tasks):
        self.store = store
        self.tasks = tasks
        self._changed = asyncio.Event()
        self._worker = None

    @property
    def is_running(self):
        return bool(self._worker and not self._worker.done())

    async def start(self):
        if not self.is_running:
            self._worker = asyncio.create_task(self._loop())

    async def close(self):
        if self._worker:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    def wake(self):
        self._changed.set()

    async def _loop(self):
        deadline = 0
        while True:
            if self.tasks.monitor:
                self.tasks.monitor.beat("automation")
            now = datetime.now(UTC).timestamp()
            if self._changed.is_set() or now >= deadline:
                self._changed.clear()
                try:
                    deadline = now + self.tick()
                except Exception:
                    logger.exception("Automation scheduler could not check the workspace")
                    deadline = now + self.heartbeat_interval
            # The durable deadline is UTC. Each heartbeat notices clock corrections
            # and time spent asleep without reading the database until work is due.
            wait = min(self.heartbeat_interval, max(0, deadline - datetime.now(UTC).timestamp()))
            with suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), wait)

    def run_now(self, identifier):
        if self.tasks.maintenance or self.tasks._closing:
            raise Conflict("维护期间不能执行规则")
        state = self.store.read()
        rule = find(state["rules"], identifier)
        if not rule:
            raise RuleNotFound("找不到规则")
        if rule["status"] == "running":
            raise Conflict("规则正在执行，请等待本次检查结束")
        if not rule["enabled"]:
            raise Conflict("请先开启规则")
        site = find(state["sites"], rule["siteId"])
        if not site or not page_origin(rule["url"]) or page_origin(site["url"]) != page_origin(rule["url"]):
            raise ValueError("规则目标与站点不匹配，请编辑后重试")

        def claim(current, scan):
            saved = find(current["rules"], identifier)
            scan["ruleId"] = identifier
            saved.update(status="running", lastRunAt=timestamp(), nextRunAt=None,
                         lastScanId=scan["id"], lastNewCount=0, lastQueuedCount=0, error="")

        scan = self.tasks.start_scan(site, {
            "url": rule["url"], "kinds": rule["kinds"], "limit": 1, "depth": 0,
            "ruleId": identifier, "readOnly": True,
        }, on_created=claim)
        self.tasks.scan_tasks[scan["id"]].add_done_callback(lambda _: self.wake())
        self.wake()
        saved = find(self.store.read()["rules"], identifier)
        return public_rule(saved)

    def _settle(self, identifier):
        def finish(state):
            rule = find(state["rules"], identifier)
            if not rule or rule.get("status") != "running":
                return
            scan = find(state["scans"], rule.get("lastScanId")) or {}
            observed = set(scan.pop("_observedAssetIds", []))
            access_error = rule.pop("_downloadAccessError", None)
            scan_status = scan.get("status")
            _trim_page_history(state["scans"], identifier)
            if (access_error or scan_status == "needs-user"
                    or scan.get("extraction", {}).get("status") == "needs-user"
                    or scan.get("_pageAccess", {}).get(rule["url"]) == "needs-user"):
                error = (access_error or scan.get("extraction", {}).get("message")
                         or (scan.get("message") if scan_status == "needs-user" else None)
                         or "请在站点窗口完成授权，再重新开启规则")
                rule.update(enabled=False, status="needs-user", nextRunAt=None, error=error)
            elif scan_status != "completed":
                error = scan.get("error") or scan.get("message") or "服务重启中断了上次检查"
                rule.update(status="failed", nextRunAt=next_check(rule), error=error)
            else:
                pending = set(rule.get("_pendingAssetIds", []))
                fresh = pending & observed
                queued = 0
                if rule["autoDownload"] and fresh:
                    ids = _downloadable_ids(state, rule, scan, observed, fresh)
                    if ids:
                        try:
                            queued = self.tasks.enqueue_in_state(state, ids, rule=rule)["added"]
                        except StoragePressure as error:
                            # Keep the pending identities for a later fresh observation.
                            rule.update(status="failed", nextRunAt=next_check(rule), error=str(error))
                            return
                rule["_pendingAssetIds"] = sorted(pending - fresh)
                rule.update(status="completed", nextRunAt=next_check(rule), error="",
                            lastNewCount=len(fresh), lastQueuedCount=queued)
            summary = (f"新增 {rule['lastNewCount']}，入队 {rule['lastQueuedCount']}"
                       if rule["status"] == "completed" else rule["error"])
            record(state, f"规则「{rule['name']}」：{summary}", "automation")

        self.store.mutate(finish)

    def tick(self):
        """Settle durable scan evidence, then start the oldest due rule, if possible."""
        if self.tasks.maintenance or self.tasks._closing:
            return 2
        state = self.store.read()
        settled = False
        for rule in state["rules"]:
            if rule.get("status") == "running" and rule.get("lastScanId") not in self.tasks.scan_tasks:
                self._settle(rule["id"])
                settled = True
        if settled:
            state = self.store.read()
        scheduled = [rule for rule in state["rules"] if rule.get("enabled") and rule.get("nextRunAt")
                     and rule.get("status") != "running"]
        if not scheduled:
            return float("inf")
        rule = min(scheduled, key=lambda row: row["nextRunAt"])
        delay = (datetime.fromisoformat(rule["nextRunAt"]) - datetime.now(UTC)).total_seconds()
        if delay > 0:
            return delay
        if self.tasks.scan_tasks:
            return 2
        try:
            self.run_now(rule["id"])
        except Conflict:
            return 2
        except ValueError as error:
            message = str(error)[:400]
            self.store.mutate(lambda state: find(state["rules"], rule["id"]).update(
                status="failed", error=message, lastRunAt=timestamp(), nextRunAt=next_check(rule),
                lastNewCount=0, lastQueuedCount=0,
            ))
        return 1

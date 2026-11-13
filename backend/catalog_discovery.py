"""Bounded, DOM-evidence-only traversal of a Lustflix episode selector."""

import asyncio
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

_SELECTOR = (
    'button[title="Episodes"], button[aria-label="Episodes"], '
    'button[title="Episode list"], button[aria-label="Episode list"]'
)


async def open_episode_selector(page, requested_url):
    """Wait briefly for a SPA control, then open only the known episode menu."""
    try:
        await page.wait_for_function(
            """selector => {
              const visible = element => !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
              return [...document.querySelectorAll(selector)].some(visible) ||
                [...document.querySelectorAll('[aria-label="Episodes"],[aria-label="Episode list"]')].some(visible);
            }""",
            arg=_SELECTOR,
            timeout=1000,
        )
    except PlaywrightTimeoutError:
        return
    if not same_page(page.url, requested_url):
        return
    selector = page.locator(_SELECTOR)
    if await selector.count() != 1 or not await selector.is_visible():
        return
    if await selector.get_attribute("aria-expanded") == "true":
        return
    await selector.click(timeout=1000)
    await page.wait_for_timeout(150)


def same_page(actual, requested):
    def identity(value):
        parsed = urlsplit(value)
        return (
            parsed.scheme,
            parsed.netloc,
            parsed.path.rstrip("/"),
            parsed.query,
            parsed.fragment.rstrip("/"),
        )

    return identity(actual) == identity(requested)


EPISODE_ROOTS_JS = r"""route => {
  if (!route) return [];
  const visible = element => !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
  return [...document.querySelectorAll('[aria-label]')].filter(element => visible(element) &&
    /^(episodes|episode list|剧集|选集)$/i.test(element.getAttribute('aria-label') || '') &&
    ![...element.querySelectorAll('a[href]')].filter(visible).some(anchor => {
      const parsed = new URL(anchor.href, location.href);
      const target = parsed.hash.match(/^#\/watch\/([A-Za-z0-9_-]+)\/\d+\/?$/);
      return target && (parsed.origin !== location.origin || target[1] !== route[1]);
    }));
}"""


EPISODE_ROWS_JS = r"""route => {
  const visible = element => !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
  const roots = (__FRAMEKEEP_EPISODE_ROOTS__)(route);
  const rows = [];
  for (const element of [...document.querySelectorAll('*')].slice(0,5000)) {
    if (!visible(element)) continue;
    const label = (element.innerText || '').trim();
    const labelMatch = label.match(/^Episode\s+(\d+)(?:\s*\n|$)/i);
    const ownId = (element.id || '').match(/^playerEpisode(\d+)$/);
    const number = Number(labelMatch?.[1] || ownId?.[1]);
    if (!number || number > 1000 || (labelMatch && ownId && Number(ownId[1]) !== number)) continue;
    if ([...label.matchAll(/^Episode\s+\d+(?:\s*\n|$)/gim)].length > 1) continue;
    const anchor = element.closest('a[href]') || element.querySelector('a[href]');
    let observedUrl = null;
    if (anchor) {
      const parsed = new URL(anchor.href, location.href);
      const target = parsed.hash.match(/^#\/watch\/([A-Za-z0-9_-]+)\/(\d+)\/?$/);
      if (parsed.origin !== location.origin || !target || target[1] !== route[1] || Number(target[2]) !== number) continue;
      observedUrl = parsed.href;
    }
    const container = element.closest('[id^="playerEpisode"]');
    const id = (container?.id || '').match(/^playerEpisode(\d+)$/);
    const scoped = roots.some(root => root.contains(element));
    if (!scoped && !observedUrl && (!id || Number(id[1]) !== number)) continue;
    rows.push({element, number, label, url:observedUrl || `${location.origin}/#/watch/${route[1]}/${number}`});
  }
  return rows;
}""".replace("__FRAMEKEEP_EPISODE_ROOTS__", EPISODE_ROOTS_JS)


_CATALOG_VIEW = r"""action => {
  const visible = element => !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
  const route = location.hash.match(/^#\/watch\/([A-Za-z0-9_-]+)\/\d+\/?$/);
  if (!route) return {links: [], counts: [], scrolls: [], pagers: [], acted: false};
  const rows = (__FRAMEKEEP_EPISODE_ROWS__)(route);
  const roots = (__FRAMEKEEP_EPISODE_ROOTS__)(route);
  const counts = [];
  for (const root of roots) {
    const explicit = Number(root.getAttribute('data-episode-count'));
    if (Number.isInteger(explicit) && explicit > 0 && explicit <= 1000) counts.push(explicit);
    for (const heading of root.querySelectorAll('h1,h2,h3,h4,[role="heading"]')) {
      const text = (heading.innerText || '').trim();
      const match = text.match(/^(?:Episodes\s*\(?\s*(\d+)\s*\)?|(\d+)\s+Episodes|共\s*(\d+)\s*集)$/i);
      const count = match && Number(match[1] || match[2] || match[3]);
      if (visible(heading) && count > 0 && count <= 1000) counts.push(count);
    }
  }
  const containers = [];
  for (const {element} of rows) {
    for (let parent = element.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
      if (!containers.includes(parent) && visible(parent) &&
          /auto|scroll/.test(getComputedStyle(parent).overflowY) &&
          parent.scrollHeight > parent.clientHeight + 2) containers.push(parent);
    }
  }
  const pagerElements = [];
  for (const root of roots) {
    for (const button of root.querySelectorAll('button,[role="button"]')) {
      const label = (button.getAttribute('aria-label') || button.title || button.innerText || '').trim();
      if (button.tagName !== 'A' && visible(button) && !button.disabled && button.getAttribute('aria-disabled') !== 'true' &&
          /^(load more(?: episodes)?|more episodes|next episodes|next page|下一页|加载更多(?:剧集)?)$/i.test(label) &&
          !pagerElements.includes(button)) pagerElements.push(button);
    }
  }
  let acted = false;
  if (action?.kind === 'scroll') {
    const element = containers[action.index];
    if (element && Math.abs(element.scrollTop - action.from) < 2) {
      const top = element.scrollTop;
      element.scrollTop = Math.min(element.scrollHeight - element.clientHeight,
        top + Math.max(40, element.clientHeight * .75));
      acted = element.scrollTop > top;
    }
  } else if (action?.kind === 'page') {
    const element = pagerElements[action.index];
    if (element) { element.click(); acted = true; }
  }
  const links = rows.map(({number,label,url}) => ({
    url, text: `Episode ${number}`,
    access: /members only|requires? (?:coins|credits)|unlock for|paid episode/i.test(label) ? 'needs-user' : 'unknown'
  }));
  return {links, counts, acted,
    scrolls: containers.map((element,index) => ({index, top:element.scrollTop, max:element.scrollHeight-element.clientHeight})),
    pagers: pagerElements.map((element,index) => ({index, label:(element.innerText || '').trim()}))};
}""".replace("__FRAMEKEEP_EPISODE_ROWS__", EPISODE_ROWS_JS).replace(
    "__FRAMEKEEP_EPISODE_ROOTS__", EPISODE_ROOTS_JS
)


async def collect_catalog(page, requested_url, cancelled, *, max_steps=20, max_seconds=8):
    """Accumulate virtualized rows without visiting or clicking an episode."""
    entries, counts, reasons = {}, set(), []
    limited = False

    async def traverse():
        nonlocal limited
        previous, stalled, action = None, 0, None
        for _ in range(max_steps):
            if cancelled():
                limited = True
                break
            if not same_page(page.url, requested_url):
                reasons.append("route-changed")
                break
            if action:
                await page.evaluate(_CATALOG_VIEW, action)
                await page.wait_for_timeout(100)
                if not same_page(page.url, requested_url):
                    reasons.append("route-changed")
                    break
            snapshot = await page.evaluate(_CATALOG_VIEW, None)
            for link in snapshot["links"]:
                prior = entries.get(link["url"])
                if prior is None or link["access"] == "needs-user":
                    entries[link["url"]] = link
            counts.update(snapshot["counts"])
            if len(entries) >= 1000:
                limited = True
                reasons.append("catalog-limit")
                break
            signature = (tuple(entries), tuple((s["top"], s["max"]) for s in snapshot["scrolls"]))
            stalled = stalled + 1 if action and signature == previous else 0
            if stalled >= 2:
                limited = True
                reasons.append("catalog-stalled")
                break
            previous = signature
            unfinished = next((s for s in snapshot["scrolls"] if s["top"] < s["max"] - 2), None)
            if unfinished:
                action = {"kind": "scroll", "index": unfinished["index"], "from": unfinished["top"]}
            elif snapshot["pagers"]:
                action = {"kind": "page", "index": snapshot["pagers"][0]["index"]}
            else:
                break
        else:
            limited = True
            reasons.append("catalog-limit")

    try:
        async with asyncio.timeout(max_seconds):
            await traverse()
    except TimeoutError:
        limited = True
        reasons.append("catalog-limit")
    if len(counts) > 1:
        reasons.append("conflicting-counts")
    return {
        "links": list(entries.values())[:1000],
        "expectedCount": next(iter(counts)) if len(counts) == 1 else None,
        "linksTruncated": limited,
        "reasons": list(dict.fromkeys(reasons)),
    }

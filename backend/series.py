"""Stable series views derived from durable scan observations and media records."""

import copy
import hashlib
import json
from urllib.parse import urlsplit

from backend.series_artwork import artwork_summary
from backend.site_adapters import (
    build_catalog,
    completed_directory_count,
    merge_catalog,
    scan_episode_outcomes,
    update_catalog,
)


def page_origin(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        return (
            parsed.scheme,
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
    except (TypeError, ValueError):
        return None


def series_identifier(scan):
    catalog = scan.get("catalog") or {}
    origin = page_origin(scan.get("url", ""))
    url = catalog.get("catalogUrl") or scan.get("url")
    if not origin or page_origin(url) != origin:
        return None
    key = catalog.get("seriesKey") or url
    identity = [scan.get("siteId"), origin, catalog.get("adapterId", "generic"), key]
    return "series_" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]


def _has_same_origin_entries(scan, entries):
    origin = page_origin(scan["url"])
    return bool(entries) and all(page_origin(entry.get("pageUrl", "")) == origin for entry in entries)


def observed_series_id(scan):
    entries = (scan.get("catalog") or {}).get("entries", [])
    identifier = series_identifier(scan)
    return identifier if identifier and _has_same_origin_entries(scan, entries) else None


def series_titles(state):
    # An explicit rename (including resetting to the source title) takes precedence.
    titles = dict(state.get("_seriesTitles", {}))
    for scan in reversed(state.get("scans", [])):
        if scan.get("_suggestedTitle"):
            identifier = observed_series_id(scan)
            if identifier:
                titles.setdefault(identifier, scan["_suggestedTitle"])
    return titles


def series_observations(state):
    """Treat each observed show as a same-origin directory in its parent scan."""
    resolved = {}
    for scan in state.get("scans", []):
        candidates = [(scan.get("url"), scan.get("catalog") or {})]
        candidates.extend((show.get("url"), show.get("catalog") or {})
                          for show in (scan.get("siteCatalog") or {}).get("shows", []))
        for url, catalog in candidates:
            if (catalog.get("entries") and page_origin(url) == page_origin(scan.get("url", ""))
                    and _has_same_origin_entries({"url": url}, catalog["entries"])):
                resolved.setdefault((scan.get("siteId"), url), catalog)
    observations = []
    aliases_by_identity = {}
    for scan in state.get("scans", []):
        observations.append(scan)  # Exact single-page outcomes still affect existing shows.
        origin = page_origin(scan.get("url", ""))
        for show in (scan.get("siteCatalog") or {}).get("shows", []):
            if not origin or page_origin(show.get("url", "")) != origin:
                continue
            catalog = copy.deepcopy(show.get("catalog") or {})
            placeholder = build_catalog(show["url"], [], series_metadata={
                "seriesKey": "generic-" + hashlib.sha256(show["url"].encode()).hexdigest()[:24],
                "catalogUrl": show["url"], "title": show.get("title") or show["url"],
            })
            alias = series_identifier({**scan, "url": show["url"], "catalog": placeholder})
            if not catalog.get("entries"):
                catalog = copy.deepcopy(resolved.get((scan.get("siteId"), show["url"]), placeholder))
            covers = list(dict.fromkeys([*catalog.get("coverUrls", []), *show.get("_indexCovers", [])]))
            if covers:
                catalog["coverUrls"] = covers[:12]
            observation = {**scan, "url": show["url"], "catalog": catalog,
                           "categories": show.get("categories", []), "_siteCatalogEntry": True,
                           "_indexOnly": not bool(catalog.get("entries")), "_seriesAliasIds": [alias]}
            aliases_by_identity.setdefault(series_identifier(observation), set()).add(alias)
            observations.append(observation)
    for scan in observations:
        aliases = aliases_by_identity.get(series_identifier(scan))
        yield {**scan, "_seriesAliasIds": sorted(aliases)} if aliases else scan


def series_library(state, *, include_dismissed=False):
    """Merge scan observations, then apply the workspace's directory selections."""
    groups = {}
    site_outcomes = {}
    hidden_series = set(state.get("_dismissedSeries", []))
    for scan in reversed(list(series_observations(state))):
        catalog = scan.get("catalog") or {}
        entries = catalog.get("entries", [])
        outcomes = site_outcomes.setdefault(scan.get("siteId"), {})
        for entry in entries:
            page = entry.get("pageUrl")
            if (
                entry.get("accessEvidence") == "account-unlock"
                and outcomes.get(page, {}).get("status") in {"needs-user", "access-pending"}
            ):
                # A later directory read found an account purchase after this last refusal.
                del outcomes[page]
        outcomes.update(scan_episode_outcomes(scan))
        identifier = series_identifier(scan)
        aliases = scan.get("_seriesAliasIds", [])
        if ((not entries and not scan.get("_indexOnly")) or not identifier
                or (not include_dismissed and hidden_series.intersection([identifier, *aliases]))):
            continue
        if entries and not _has_same_origin_entries(scan, entries):
            continue
        url = catalog.get("catalogUrl") or scan["url"]
        key = catalog.get("seriesKey") or url
        prior = groups.pop(identifier, None)
        merged = (merge_catalog(prior["catalog"], catalog, directory_count=completed_directory_count(scan))
                  if prior else copy.deepcopy(catalog))
        # Snapshot access can be inferred from old assets. Only this scan's exact
        # page observation may supersede an older permission gate.
        observed = {entry["episodeKey"]: entry for entry in entries}
        page_access = scan.get("_pageAccess", {})
        for entry in merged.get("entries", []):
            latest = observed.get(entry["episodeKey"], {})
            if latest.get("pageUrl") != entry.get("pageUrl"):
                continue
            checked_access = page_access.get(entry.get("pageUrl"))
            if latest.get("access") != "needs-user" and checked_access == "observed":
                entry["access"] = "observed"
            elif (catalog.get("adapterId") == "chunlou" and latest.get("access") == "needs-user"
                  and checked_access == "needs-user"):
                # Older snapshots stored verified player denials only in the exact-page map.
                entry["accessEvidence"] = "player"
        for field in ("title", "catalogUrl"):
            if catalog.get(field):
                merged[field] = catalog[field]
        source_scan_ids = prior["sourceScanIds"] if prior else []
        if scan["id"] not in source_scan_ids:
            source_scan_ids.append(scan["id"])
        groups[identifier] = {
            "id": identifier,
            "siteId": scan.get("siteId"),
            "url": url,
            "title": merged.get("title") or str(key),
            "scanId": scan["id"],
            "sourceScanIds": source_scan_ids,
            "status": scan.get("status", "completed"),
            "message": scan.get("message", ""),
            "updatedAt": scan.get("finishedAt") or scan.get("createdAt", ""),
            "catalog": merged,
            "directoryPending": not bool(merged.get("entries")),
            "_seriesAliasIds": list(dict.fromkeys([*(prior or {}).get("_seriesAliasIds", []), *aliases])),
            "categories": list(dict.fromkeys([*(prior or {}).get("categories", []),
                                              *scan.get("categories", [])])),
        }
    result = list(reversed(groups.values()))
    titles = series_titles(state)
    for row in result:
        row["posterUrl"] = f"/api/series/{row['id']}/poster"
        row["sourceTitle"] = row["title"]
        row["customTitle"] = next((titles[key] for key in [row["id"], *row["_seriesAliasIds"]]
                                  if key in titles), "")
        row["title"] = row["customTitle"] or row["sourceTitle"]
        assets = [
            asset for asset in state.get("assets", []) if asset.get("siteId") == row["siteId"]
        ]
        row["artwork"] = artwork_summary(row, assets)
        excluded = (
            () if include_dismissed else state.get("_dismissedEpisodes", {}).get(row["id"], ())
        )
        row["catalog"] = update_catalog(
            row["catalog"], assets, dismissed_keys=excluded, episode_outcomes=site_outcomes[row["siteId"]],
        )
    return result


def annotate_series_scans(state):
    """Annotate the public copy, including earlier failed attempts at the same URL."""
    hidden = set(state.get("_dismissedSeries", []))
    state["dismissedSeriesCount"] = len(hidden)
    titles = series_titles(state)
    if not hidden and not titles:
        return
    hidden_pages = set()
    for scan in state["scans"]:
        identifier = series_identifier(scan)
        catalog = scan.get("catalog") or {}
        if titles.get(identifier) and _has_same_origin_entries(scan, catalog.get("entries", [])):
            scan["customSeriesTitle"] = titles[identifier]
        if identifier in hidden:
            scan["seriesDismissed"] = True
            if _has_same_origin_entries(scan, catalog.get("entries", [])):
                scan["discoverySeriesId"] = identifier
            pages = [scan["url"], catalog.get("catalogUrl")]
            pages.extend(entry.get("pageUrl") for entry in catalog.get("entries", []))
            hidden_pages.update((scan["siteId"], url) for url in pages if url)
    for scan in state["scans"]:
        if (
            scan.get("mode") == "series"
            and not (scan.get("catalog") or {}).get("entries")
            and (scan.get("siteId"), scan.get("url")) in hidden_pages
        ):
            scan["seriesDismissed"] = True


def restore_scanned_series(state, scan, options):
    """Undo a previous whole-show removal only for this explicitly requested directory."""
    found = observed_series_id(scan)
    hidden = state.get("_dismissedSeries", [])
    if not found:
        return None
    aliases = {found}
    for observation in series_observations(state):
        if series_identifier(observation) == found:
            aliases.update(observation.get("_seriesAliasIds", []))
    restored = aliases.intersection(options["restoreCandidates"], hidden)
    if not restored:
        return None
    catalog = scan["catalog"]
    if not options.get("knownRestoreTarget") and (
        scan["url"] != catalog.get("catalogUrl")
        and not any(entry["pageUrl"] == scan["url"] for entry in catalog["entries"])
    ):
        return None
    hidden[:] = [key for key in hidden if key not in restored]
    return found


def dismiss_series(state, ids, episode_keys):
    selected = set(ids)
    if episode_keys is not None and len(selected) != 1:
        raise ValueError("移除选集时仅可选择一部剧")
    rows = [row for row in series_library(state) if row["id"] in selected]
    if not rows:
        return 0
    if episode_keys is None:
        hidden = state.setdefault("_dismissedSeries", [])
        hidden.extend(row["id"] for row in rows)
        return len(rows)
    row = rows[0]
    wanted = set(episode_keys)
    keys = [
        entry["episodeKey"] for entry in row["catalog"]["entries"] if entry["episodeKey"] in wanted
    ]
    if keys:
        state.setdefault("_dismissedEpisodes", {}).setdefault(row["id"], []).extend(keys)
    return len(keys)

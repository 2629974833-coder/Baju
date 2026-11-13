"""Small evidence-based site adapters; unknown sites retain exact URL identity."""

import copy
import hashlib
import json
import re
from urllib.parse import parse_qsl, urljoin, urlsplit

from backend.chunlou import episode_route as chunlou_episode
from backend.chunlou import manifest_url_matches as chunlou_manifest
from backend.classification import automatic_classification, lustflix_stream_matches
from backend.short_media import short_identity

_LUSTFLIX_HOSTS = {"lustflix.tv", "www.lustflix.tv"}
_SIGNATURE_PARAMETERS = {"token", "signature", "sig", "expires", "expire", "exp"}
_WATCH = re.compile(r"/watch/([A-Za-z0-9_-]+)/(\d+)")
_MEDIA = re.compile(r"/api/media/([A-Za-z0-9_-]+)/(\d+)/(master|stream_\d+|stream_audio\d+)\.m3u8")
_PLAYBACK_MEDIA = re.compile(
    r"/api/playback-media/([A-Za-z0-9._~-]+)/([A-Za-z0-9_-]+)/(\d+)/"
    r"(master|stream|stream_\d+|stream_audio\d+)\.m3u8"
)
_MAX_EPISODES = 1000
_UNSUPPORTED_MEDIA = {"unsupported-media", "unsupported-drm"}
_FAILED_PLAYBACK = _UNSUPPORTED_MEDIA | {"failed"}
_EPISODE_OUTCOMES = _FAILED_PLAYBACK | {"ready", "needs-user", "access-pending", "unresolved"}
_OBSERVATION_WARNINGS = {
    "catalog-limit",
    "catalog-stalled",
    "page-limit",
    "truncated",
    "route-changed",
    "read-only-blocked",
}


def _url(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        parsed.port  # Validate malformed ports without making a network request.
        return parsed
    except (TypeError, ValueError, AttributeError):
        return None


def _origin(parsed):
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def _lustflix_page(value):
    parsed = _url(value)
    if not parsed or parsed.hostname not in _LUSTFLIX_HOSTS:
        return None
    return _WATCH.fullmatch(parsed.fragment.rstrip("/"))


def stable_asset_identity(asset):
    """Return a private identity only for the observed, explicitly known media route.

    Only recognized signature fields and the known playback signature segment may
    rotate. Unknown CDNs and other sites never lose their queries or path segments.
    """
    if asset.get("shortMedia") and _url(asset.get("sourceUrl")):
        return short_identity(asset)
    source = _url(asset.get("sourceUrl"))
    page_url = asset.get("pageUrl")
    page = _url(page_url)
    if identifier := asset.get("_chunlouEpisode"):
        if (asset.get("kind") != "video" or asset.get("engine") != "yt-dlp" or not source
                or chunlou_episode(page_url) != identifier or not chunlou_manifest(asset["sourceUrl"], identifier)
                or not source.path.endswith("/index.m3u8")):
            return None
        pairs = parse_qsl(source.query, keep_blank_values=True)
        # The observed Chunlou rendition URLs sign this exact path with e/n/s together.
        transient = _SIGNATURE_PARAMETERS
        if {"e", "n", "s"}.issubset({k for k, _ in pairs}):
            transient = transient | {"e", "n", "s"}
        query = [(k, v) for k, v in pairs if k.lower() not in transient]
        identity = ["chunlou-v1", identifier, source.path, query]
        return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    route = _lustflix_page(page_url)
    if (
        asset.get("kind") != "video"
        or asset.get("engine") != "yt-dlp"
        or not source
        or not page
        or not route
        or source.scheme != "https"
        or page.scheme != "https"
        or source.hostname not in _LUSTFLIX_HOSTS
        or source.port not in {None, 443}
        or page.port not in {None, 443}
    ):
        return None
    media = _MEDIA.fullmatch(source.path)
    playback = _PLAYBACK_MEDIA.fullmatch(source.path)
    number = media[2] if media else playback[3] if playback else None
    if source.path.endswith("/stream.m3u8"):
        if not lustflix_stream_matches(source, page):
            return None
    elif number != route[2]:
        return None
    identity_path = source.path
    if playback:
        identity_path = (
            f"/api/playback-media/{{signature}}/{playback[2]}/{playback[3]}/{playback[4]}.m3u8"
        )
    query = [
        (key, value)
        for key, value in parse_qsl(source.query, keep_blank_values=True)
        if key.lower() not in _SIGNATURE_PARAMETERS
    ]
    identity = ["lustflix-v1", page_url, source.hostname, identity_path, query]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def refresh_identity_matches(old, new):
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    if not _url(old.get("sourceUrl")) or not _url(old.get("pageUrl")):
        return False
    if any(old.get(key) != new.get(key) for key in ("pageUrl", "kind", "engine")):
        return False
    if new.get("unsupported") or new.get("locked"):
        return False
    if (
        old.get("kind") == "video"
        and automatic_classification(old)["kind"] != automatic_classification(new)["kind"]
    ):
        return False
    old_identity, new_identity = stable_asset_identity(old), stable_asset_identity(new)
    if old_identity is not None:
        return old_identity == new_identity
    return old.get("sourceUrl") == new.get("sourceUrl")


def build_catalog(
    page_url, links, *, expected_count=None, truncated=False, reasons=None, series_metadata=None
):
    """Build a bounded catalog from visible links, without guessing unobserved URLs."""
    page = _url(page_url)
    is_lustflix = bool(page and page.hostname in _LUSTFLIX_HOSTS)
    series = None
    metadata = {}
    if is_lustflix:
        current = re.fullmatch(
            r"/(?:watch|series|detail|s)/([A-Za-z0-9_-]+)(?:/\d+)?", page.fragment.rstrip("/")
        )
        if current:
            series = current[1]
    elif page and isinstance(series_metadata, dict):
        key = series_metadata.get("seriesKey")
        catalog_url = _url(series_metadata.get("catalogUrl"))
        if (
            isinstance(key, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", key)
            and catalog_url
            and _origin(page) == _origin(catalog_url)
        ):
            series = key
            metadata = {
                "catalogUrl": series_metadata["catalogUrl"],
                "title": str(series_metadata.get("title") or "")[:200],
            }
    normalized = []
    ordinal_limited = False
    for link in links[:3000]:
        value = link.get("pageUrl", link.get("url", ""))
        url = urljoin(page_url, value) if isinstance(value, str) else ""
        parsed = _url(url)
        if not parsed or not page or parsed.hostname != page.hostname:
            continue
        if not is_lustflix and _origin(parsed) != _origin(page):
            continue
        route = _lustflix_page(url) if is_lustflix else None
        if is_lustflix and not route:
            continue
        ordinal = int(route[2]) if route else link.get("ordinal")
        if not route and (type(ordinal) is not int or ordinal < 1):
            continue
        if route and (int(route[2]) < 1 or (series and route[1] != series)):
            continue
        if ordinal > _MAX_EPISODES:
            ordinal_limited = True
            continue
        normalized.append((link, url, route, ordinal))
    if is_lustflix and not series:
        observed_series = {route[1] for _, _, route, _ in normalized if route}
        if len(observed_series) == 1:
            series = next(iter(observed_series))
        else:
            normalized = []  # Mixed recommendation links do not establish a series.
    entries, seen, duplicates = [], {}, 0
    conflicting_pages = False
    for link, url, route, ordinal in normalized:
        key = f"{series}:{ordinal}" if series else url
        access = link.get("access", "unknown")
        if access not in {"unknown", "observed", "needs-user"}:
            access = "unknown"
        if key in seen:
            duplicates += 1
            if not is_lustflix and series and seen[key]["pageUrl"] != url:
                conflicting_pages = True
            if access == "needs-user":
                seen[key]["access"] = access
            continue
        entry = {
            "episodeKey": key,
            "pageUrl": url,
            "title": str(link.get("title", link.get("text", "")))[:200],
            "ordinal": ordinal,
            "access": access,
            "assetIds": [],
        }
        seen[key] = entry
        entries.append(entry)
    expected = (
        expected_count if type(expected_count) is int and 0 < expected_count <= 1000 else None
    )
    catalog = {
        **metadata,
        "adapterId": "lustflix" if is_lustflix else "generic",
        "scope": "series" if series else "page",
        "seriesKey": series,
        "expectedCount": expected,
        "entries": sorted(entries, key=lambda entry: entry["ordinal"]),
        "duplicateCount": duplicates,
        "truncated": bool(truncated or len(links) > 3000 or ordinal_limited),
        "reasons": list(
            dict.fromkeys(
                [
                    *(reasons or []),
                    *(["ordinal-limit"] if ordinal_limited else []),
                    *(["catalog-conflict"] if conflicting_pages else []),
                ]
            )
        ),
    }
    return update_catalog(catalog, [])


def _catalog_origins(catalog):
    return {
        _origin(parsed)
        for value in [
            catalog.get("catalogUrl"),
            *[entry.get("pageUrl") for entry in catalog.get("entries", [])],
        ]
        if (parsed := _url(value)) is not None
    }


def completed_directory_count(scan):
    """Only a successful, standalone directory read can supersede a crawl warning."""
    if scan.get("mode") != "series" or scan.get("status") != "completed":
        return None
    return scan.get("_officialDirectoryCount")


def scan_episode_outcomes(scan):
    """Read exact-page playback observations; a directory summary can describe multiple episodes."""
    outcomes = scan.get("_episodeOutcomes", {})
    if not outcomes and scan.get("mode") == "single" and scan.get("status") in _UNSUPPORTED_MEDIA:
        url = scan.get("url", "")
        if chunlou_episode(url):
            # Previous releases recorded this evidence only on the single-episode scan.
            outcomes = {url: {"status": scan["status"], "message": scan.get("message", "")}}
    page_access = scan.get("_pageAccess", {})
    return {
        url: {**outcome, "access": page_access.get(url)}
        for url, outcome in outcomes.items()
    }


def _without_unpublished(catalog, evidence):
    if not catalog or not evidence or not evidence.get("excludedPages"):
        return catalog
    pages = set(evidence["excludedPages"])
    trimmed = {**catalog, "entries": [row for row in catalog.get("entries", [])
                                      if row["pageUrl"] not in pages]}
    # Old directories counted the trailing unpublished slots as part of the total.
    # Only remove exact slot numbers attributed to this same show by the new picker.
    total = catalog.get("expectedCount")
    if total:
        slots = {int(route[2]) for url in pages if (route := _lustflix_page(url))}
        slots.difference_update(row["ordinal"] for row in trimmed["entries"])
        while total in slots:
            total -= 1
        trimmed["expectedCount"] = total if total and not any(n < total for n in slots) else None
    return trimmed


def directory_covers_prior(previous, current, count):
    previous = _without_unpublished(previous, current.get("_publicationRead"))
    if (type(count) is not int or not 1 <= count <= _MAX_EPISODES
            or current.get("adapterId") != "lustflix"
            or "site-directory" not in current.get("discoverySources", [])
            or current.get("expectedCount") != count
            or ((previous or {}).get("expectedCount") or 0) > count):
        return False
    rows = current.get("entries", [])
    pages = {entry["ordinal"]: entry["pageUrl"] for entry in rows}
    return (len(rows) == count and set(pages) == set(range(1, count + 1))
            and all(pages.get(entry["ordinal"]) == entry["pageUrl"]
                    for entry in (previous or {}).get("entries", [])))


def merge_catalog(previous, current, *, observed_page=None, access="unknown", directory_count=None):
    """Merge observations within a scope; only exact-page success clears an old gate."""
    if not previous or not (previous.get("seriesKey") or previous.get("entries")):
        merged = copy.deepcopy(current)
    else:
        old_origins, new_origins = _catalog_origins(previous), _catalog_origins(current)
        if (
            previous.get("adapterId") != current.get("adapterId")
            or previous.get("seriesKey") != current.get("seriesKey")
            or (old_origins and new_origins and old_origins != new_origins)
            or (
                previous.get("catalogUrl")
                and current.get("catalogUrl")
                and previous["catalogUrl"] != current["catalogUrl"]
            )
        ):
            return copy.deepcopy(previous)
        # Seeded single-page scans can finish after a newer directory read. Use the
        # release snapshot's own timestamp to keep unpublished slots out of both inputs.
        latest = max((previous, current), key=lambda cat: cat.get("_publicationRead", {}).get("at", 0))
        release = latest.get("_publicationRead")
        previous = _without_unpublished(previous, release)
        current = _without_unpublished(current, release)
        merged = copy.deepcopy(previous)
        if release:
            merged["_publicationRead"] = copy.deepcopy(release)
            merged["publishedCount"] = latest["publishedCount"]
            merged["ongoing"] = latest["ongoing"]
        if current.get("coverUrls"):
            merged["coverUrls"] = list(current["coverUrls"][:12])
        if current.get("title"):
            merged["title"] = current["title"]
        if not merged.get("catalogUrl") and current.get("catalogUrl"):
            merged["catalogUrl"] = current["catalogUrl"]
        entries = {entry["episodeKey"]: entry for entry in merged.get("entries", [])}
        conflicting_pages = False
        for entry in current.get("entries", []):
            old = entries.get(entry["episodeKey"])
            if old is None:
                entries[entry["episodeKey"]] = copy.deepcopy(entry)
            else:
                if (
                    merged.get("adapterId") == "generic"
                    and merged.get("seriesKey")
                    and old.get("pageUrl") != entry.get("pageUrl")
                ):
                    conflicting_pages = True
                if entry.get("access") == "needs-user":
                    public_gate = entry.get("accessEvidence") == "public-directory"
                    # Anonymous prices cannot revoke exact-page or local-file evidence.
                    if not public_gate or not (old.get("access") == "observed" or old.get("downloaded")
                                               or old.get("mediaStatus") in {"ready", "downloaded"}):
                        legacy_price_hint = merged.get("adapterId") == "chunlou" and not old.get("accessEvidence")
                        if public_gate and (old.get("access") != "needs-user" or legacy_price_hint):
                            old["accessEvidence"] = "public-directory"
                        old["access"] = "needs-user"
                        if not public_gate:
                            if entry.get("accessEvidence"):
                                old["accessEvidence"] = entry["accessEvidence"]
                            else:
                                old.pop("accessEvidence", None)
                elif entry.get("accessEvidence") == "account-unlock":
                    old["access"] = entry["access"]
                    old["accessEvidence"] = "account-unlock"
                    if old.get("scanStatus") in {"needs-user", "access-pending"}:
                        old.pop("scanStatus", None)
                        old.pop("scanMessage", None)
                elif old.get("access") != "needs-user" and entry.get("access") == "observed":
                    old["access"] = "observed"
        merged["entries"] = sorted(entries.values(), key=lambda entry: entry["ordinal"])
        merged["reasons"] = list(
            dict.fromkeys(
                [
                    *merged.get("reasons", []),
                    *current.get("reasons", []),
                    *(["catalog-conflict"] if conflicting_pages else []),
                ]
            )
        )
        total = current.get("expectedCount")
        if total is not None:
            if merged.get("expectedCount") not in {None, total}:
                merged["reasons"].append("conflicting-counts")
            else:
                merged["expectedCount"] = total
        merged["truncated"] = bool(merged.get("truncated") or current.get("truncated"))
        # Progress snapshots can be merged repeatedly; this is the highest observed
        # in-page duplicate count, not a count of merging the same evidence again.
        merged["duplicateCount"] = max(
            merged.get("duplicateCount", 0), current.get("duplicateCount", 0)
        )
    merged["discoverySources"] = list(dict.fromkeys(
        source for source in [*(previous or {}).get("discoverySources", []),
                              *current.get("discoverySources", [])]
        if source in {"dom", "json-ld", "site-directory"}
    ))
    if directory_covers_prior(previous, current, directory_count):
        # This read enumerated every ordinal. Keep media/access state, but retire obsolete
        # page-traversal warnings and accept newly published episodes with matching identities.
        merged["truncated"] = False
        merged["expectedCount"] = directory_count
        merged["reasons"] = [r for r in merged.get("reasons", [])
                             if r not in _OBSERVATION_WARNINGS and r != "conflicting-counts"]
    current_gates = {
        entry["pageUrl"]
        for entry in current.get("entries", [])
        if entry.get("access") == "needs-user"
    }
    for entry in merged.get("entries", []):
        if entry.get("pageUrl") != observed_page:
            continue
        if access == "needs-user" or (access == "observed" and observed_page not in current_gates):
            entry["access"] = access
    return update_catalog(merged, [])


def _episode_assets(entry, assets):
    requires_identity = bool(_lustflix_page(entry.get("pageUrl")) or chunlou_episode(entry.get("pageUrl")))
    matches = []
    for asset in assets:
        if (
            asset.get("kind") != "video"
            or not asset.get("id")
            or asset.get("pageUrl") != entry.get("pageUrl")
            or not _url(asset.get("sourceUrl"))
            or asset.get("unsupported")
            or asset.get("locked")
            or automatic_classification(asset)["kind"] != "episode"
        ):
            continue
        if requires_identity and not stable_asset_identity(asset):
            continue
        evidence = asset.get("_episodeEvidence")
        if (
            isinstance(evidence, dict)
            and evidence.get("adapterId") == "generic"
            and entry.get("episodeKey") != entry.get("pageUrl")
            and (
                evidence.get("episodeKey") != entry.get("episodeKey")
                or evidence.get("ordinal") != entry.get("ordinal")
            )
        ):
            continue
        matches.append(asset)
    return matches


def select_episode_asset(entry, assets):
    """Choose one observed primary media identity, without weakening refresh identity."""
    scan_status = entry.get("scanStatus")
    if entry.get("access") == "needs-user":
        if entry.get("accessEvidence") == "public-directory" and scan_status in _FAILED_PLAYBACK:
            return {"assetId": None, "state": scan_status}
        return {"assetId": None, "state": "needs-user"}
    candidates = _episode_assets(entry, assets)
    if scan_status in _UNSUPPORTED_MEDIA:
        # A new exact-page observation can invalidate a previously discovered URL.
        # An original already saved from that page remains available for export.
        candidates = [asset for asset in candidates if asset.get("status") == "downloaded"]
    if not candidates and scan_status in _FAILED_PLAYBACK:
        return {"assetId": None, "state": scan_status}
    families = set()
    for candidate in candidates:
        source = urlsplit(candidate["sourceUrl"])
        family = (candidate.get("engine"), candidate["sourceUrl"])
        if _lustflix_page(entry.get("pageUrl")) and stable_asset_identity(candidate):
            # Only canonical selection groups roles within a verified media family.
            # Refresh identities must still retain master/variant/audio roles.
            master_path = source.path.rsplit("/", 1)[0] + "/master.m3u8"
            family = stable_asset_identity(
                {**candidate, "sourceUrl": source._replace(path=master_path).geturl()}
            )
        families.add(family)
    if len(families) > 1:
        return {"assetId": None, "state": "ambiguous"}
    masters = [
        asset for asset in candidates if urlsplit(asset["sourceUrl"]).path.endswith("/master.m3u8")
    ]
    choices = masters if masters else candidates
    identities = {}
    for candidate in choices:
        identity = stable_asset_identity(candidate) or (
            candidate.get("engine"),
            candidate["sourceUrl"],
        )
        previous = identities.get(identity)
        if previous is None or (
            candidate.get("status") == "downloaded" and previous.get("status") != "downloaded"
        ):
            identities[identity] = candidate
    if not identities:
        return {"assetId": None, "state": "unresolved"}
    if len(identities) != 1:
        return {"assetId": None, "state": "ambiguous"}
    selected = next(iter(identities.values()))
    return {
        "assetId": selected["id"],
        "state": "downloaded" if selected.get("status") == "downloaded" else "ready",
    }


def update_catalog(catalog, assets, *, dismissed_keys=(), episode_outcomes=None):
    """Recompute coverage, primary media choice and independent local-file evidence."""
    result = copy.deepcopy(catalog)
    observed_entries = result.get("entries", [])
    entries = observed_entries
    if dismissed_keys:
        excluded = set(dismissed_keys)
        entries = [entry for entry in observed_entries if entry["episodeKey"] not in excluded]
        result["entries"] = entries
        result["dismissedEpisodeCount"] = len(observed_entries) - len(entries)
    assets_by_page = {}
    for asset in assets:
        assets_by_page.setdefault(asset.get("pageUrl"), []).append(asset)
    for entry in entries:
        outcome = (episode_outcomes or {}).get(entry.get("pageUrl"))
        if outcome and outcome.get("status") in _EPISODE_OUTCOMES:
            entry["scanStatus"] = outcome["status"]
            entry["scanMessage"] = outcome.get("message", "")
            if outcome["status"] == "access-pending" and result.get("adapterId") == "chunlou":
                entry.update(access="needs-user", accessEvidence="public-directory")
            elif outcome.get("access") in {"observed", "needs-user"}:
                entry["access"] = outcome["access"]
                if outcome["access"] != "observed" or entry.get("accessEvidence") != "account-unlock":
                    entry["accessEvidence"] = "player"
        matches = _episode_assets(entry, assets_by_page.get(entry.get("pageUrl"), []))
        choice = select_episode_asset(entry, matches)
        saved = select_episode_asset({**entry, "access": "unknown"}, matches)
        entry["assetIds"] = list(dict.fromkeys(asset["id"] for asset in matches))
        entry["downloadAssetId"] = choice["assetId"]
        entry["mediaStatus"] = choice["state"]
        entry["downloaded"] = saved["state"] == "downloaded"
        if matches and entry.get("access") != "needs-user" and choice["state"] not in _FAILED_PLAYBACK:
            entry["access"] = "observed"
    count = len(entries)
    expected = result.get("expectedCount")
    resolved = sum(entry["mediaStatus"] in {"ready", "downloaded"} for entry in entries)
    blocked = sum(entry.get("access") == "needs-user" for entry in entries)
    ambiguous = sum(entry["mediaStatus"] == "ambiguous" for entry in entries)
    unsupported = sum(entry["mediaStatus"] in _UNSUPPORTED_MEDIA for entry in entries)
    failed = sum(entry["mediaStatus"] == "failed" for entry in entries)
    missing = []
    present = {entry["ordinal"] for entry in observed_entries if type(entry.get("ordinal")) is int}
    bounded = {number for number in present if 0 < number <= _MAX_EPISODES}
    observed_gaps = (
        [number for number in range(min(bounded), max(bounded)) if number not in bounded]
        if bounded
        else []
    )
    outside = sorted(number for number in present if expected is not None and number > expected)
    if expected and result.get("seriesKey"):
        missing = [
            f"{result['seriesKey']}:{number}"
            for number in range(1, expected + 1)
            if number not in present
        ]
    reasons = [
        reason
        for reason in result.get("reasons", [])
        if reason
        not in {
            "missing-entries",
            "unresolved-media",
            "unsupported-media",
            "scan-failed",
            "blocked-entries",
            "truncated",
            "ambiguous-media",
            "out-of-range",
        }
    ]
    if expected is not None and (len(observed_entries) != expected or missing):
        reasons.append("missing-entries")
    if any(entry["mediaStatus"] == "unresolved" for entry in entries):
        reasons.append("unresolved-media")
    if unsupported:
        reasons.append("unsupported-media")
    if failed:
        reasons.append("scan-failed")
    if blocked:
        reasons.append("blocked-entries")
    if ambiguous:
        reasons.append("ambiguous-media")
    if outside:
        reasons.append("out-of-range")
    if result.get("truncated"):
        reasons.append("truncated")
    # Keep past observation warnings visible without making recovery impossible.
    # Actual coverage, current gates and identity/count conflicts govern completeness.
    complete = bool(
        expected is not None
        and count == len(observed_entries) == expected
        and present == set(range(1, expected + 1))
        and resolved == count
        and not missing
        and not blocked
        and not ambiguous
        and not outside
        and not any(reason not in _OBSERVATION_WARNINGS for reason in reasons)
    )
    status = "unknown" if expected is None else "partial"
    if complete:
        status = "complete-observed"
    missing_count = None
    if expected is not None:
        missing_count = max(0, expected - len(observed_entries))
        if result.get("seriesKey"):
            missing_count = len(missing)
    result.update(
        enumeratedCount=count,
        resolvedCount=resolved,
        downloadedCount=sum(entry["downloaded"] for entry in entries),
        blockedCount=blocked,
        ambiguousCount=ambiguous,
        unsupportedCount=unsupported,
        failedCount=failed,
        missingEpisodeKeys=missing,
        missingCount=missing_count,
        observedGapOrdinals=observed_gaps,
        outOfRangeOrdinals=outside,
        reasons=list(dict.fromkeys(reasons)),
        status=status,
    )
    return result

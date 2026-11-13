# Stage 1 series collection implementation plan

> For agentic workers: use test-driven implementation, isolated file ownership, and independent spec/code review. The user approved the four-stage roadmap and requested all observed episodes of Lustflix C03. Preserve the existing working tree, saved sessions and original files; no automatic commits.

**Goal:** Enumerate an observed series, show missing/blocked/ambiguous entries, select one canonical media per episode, and download/export every accessible episode with useful names.

**Architecture:** Extend existing scan catalogs and persisted tasks, without adding another series database or replacing media identity. Collect only visible DOM evidence with bounded traversal. Keep immutable original storage; export safe named files through standard-library ZIP streaming.

**Tech stack:** Existing Python/FastAPI/SQLite/Playwright/yt-dlp/FFmpeg and native JavaScript; Python zipfile for stored-media exports.

## Scope and acceptance

Target supplied by user: `https://lustflix.tv/#/watch/C03/5`; all episodes from first to last that the page actually establishes. User has not yet confirmed the new session, manual unlock or disabled automatic unlock. Do not visit potentially paid episodes with that account until confirmation. Do not spend coins, purchase, change site payment settings or infer permissions. Finish controlled engineering and testing while waiting.

## 1. Catalog semantics — adapter agent

Files: backend/site_adapters.py; new tests_backend/test_series_catalog.py.

- [x] RED: same-series union, locked duplicate priority, exact-page unlock evidence, total conflicts, out-of-range ordinal and missing counts, canonical media identity/ambiguity.
- [x] Add `merge_catalog(previous,current,*,observed_page=None,access='unknown')`. Same adapter and series only; unknown totals stay unknown, no synthesized page URLs. Exact successful observation can clear that entry's previous permission block. Current blocked duplicate wins over unknown.
- [x] Add `select_episode_asset(entry,assets)` returning `{assetId,state}` where state is ready/downloaded/needs-user/unresolved/ambiguous. Auto classification and same page required; prefer master; deduplicate recognized signed identity, downloaded original ID preferred; true identity conflicts are ambiguous.
- [x] `update_catalog` adds entry `downloadAssetId/mediaStatus`, `ambiguousCount`, `observedGapOrdinals`, correct `missingCount`, preserves existing compatible fields. Coverage never claims complete with contradictory totals or out-of-range entries.

## 2. Bounded read-only discovery — scanner agent

Files: backend/scanner.py; new backend/catalog_discovery.py; tests_backend/test_series_scanning.py.

- [x] RED: real local Chromium fixtures with scrolling/virtualized lists, next/load-more only within episode controls, cancellation, limits, mixed series, blocked requests and route changes.
- [x] Collect only actually observed links/episode rows, up to 20 rounds / 8 seconds / 1000 entries. Merge snapshots rather than final DOM only. Do not click episodes or unlock controls. Explicit scoped total only; report stalled/limited/conflicting evidence.
- [x] Lustflix independent scan context blocks non-GET/HEAD/OPTIONS and WebSockets including popup first requests while retaining the existing network policy; media cannot autoplay. Saved sessions restore without authorization-page navigation; yt-dlp extraction is read-only for Lustflix too. A route change discards candidates instead of attributing them to the wrong episode. User authorization browser stays unchanged.
- [x] Use shared `merge_catalog`, skip known needs-user BFS links and seed entries, attach conservative reasons. Blocking analytics alone must not discard valid GET media.

## 3. Task/API integration and exports — root

Files: backend/app.py, backend/tasks.py, new backend/archive.py, tests_backend/test_series_api.py.

- [x] RED: fresh scan inherits only same-site/same-series catalog; new observations remain merged; selected scan skips locked entries with detailed response; canonical episode downloads skip already-saved identities and explain unavailable reasons.
- [x] Wire shared helpers in scan start/progress/API. Preserve frozen download options and per-asset locks. No global collapse of independently selected quality variants.
- [x] RED: safe names, Unicode/control/path traversal, numbered episodes, original protection, ZIP round-trip bytes/SHA and manifest of excluded entries, bounded chunk writes, cancellation.
- [x] `download_filename(asset,site,entry=None,catalog=None)` produces safe names such as `Lustflix_C03_E005_720p.mp4`, without moving original files. Existing file endpoint uses it for attachment names.
- [x] `GET /api/scans/{id}/archive` streams only existing canonical downloaded files, named under site/series directories, plus a manifest containing statuses, hashes and absent entries. No signed source URLs or credentials. If no files are saved return409. Use ZIP_STORED and bounded chunks, no duplicate full archive on disk.

## 4. Catalog panel — UI agent

Files: dist/catalog-view.js, relevant dist/app.js handlers, added CSS and tests/catalog-view.test.mjs.

- [x] RED/GREEN: show known total, exact missing ordinals versus observed gaps, blocked/ambiguous/ready/saved counts. Preserve unknown total language and escape metadata.
- [x] Add convenient selection of unresolved and available entries without selecting locked entries for automatic scans; add named ZIP export for downloaded series. Retain manual per-entry selection and clear returned skipped counts.
- [x] Render details for read-only requests/route drift/traversal limits in user-facing language. Do not imply payment completion or full-series coverage without evidence.

## 5. Integration and rollout — root/reviewer

- [x] Full backend/frontend/Ruff/syntax; controlled multi-episode series scan → batch download → restart persistence → ZIP export bytes/decode. Independent review of completeness, permissions, output naming and original preservation; small code-simplifier pass.
- [x] CUA panel interaction, ZIP download bytes and screenshots using isolated controlled fixtures.
- [ ] Saved C03 session only after user confirms it is ready; inspect actual directory, scan only unlocked/accessible observed pages, download all such canonical episodes, verify every file and missing/blocked inventory. Do not declare full series complete with unresolved entries.
- [ ] Do not restart the main4173 service while user may be logging in with unsaved state. After saved-session confirmation and checks, back up encrypted state/SQLite, restart, verify both localhost hosts and all original hashes. If account readiness blocks live acceptance, report that specific gate without treating tests as real account success.
- [x] Record measured evidence in docs/STAGE1_SERIES.md and update QA/roadmap. Watermarks, server/container repair, other three accounts and scheduled jobs remain later stages.

## Recorded outcome

353 backend tests, 63 frontend tests, Ruff/syntax/diff checks passed. Controlled series acceptance passed in 9.049s. Anonymous real C03/5 scan returned needs-user, zero catalog entries/assets and unknown last episode. Main service remains the previous running version to preserve user authorization window; original two SHA256 hashes unchanged. Live account readiness and full C03 downloads are pending, so Stage 1 is not fully accepted. See docs/STAGE1_SERIES.md.

## Superseding live outcome

The user explicitly authorized spending existing coins to unlock C03. Unlocks were performed (57 coins spent, balance 183). After the public detail page established explicit sexual content, further retrieval was stopped. No C03 episode files were downloaded; there are no active scan/download jobs. The live acceptance target is no longer awaiting user permission and must not be resumed for this content. General engineering work can be validated with non-explicit authorized samples. See docs/STAGE1_SERIES.md and test-output/c03-live-20261103/outcome.json.

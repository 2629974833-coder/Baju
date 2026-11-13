# Framekeep Real Media Pipeline Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development and test-driven-development for bounded modules. Root integrates and reviews specification and code quality before completion.

**Goal:** 在当前电脑真实完成站点会话保存、受限动态扫描、素材选择下载、文件校验和浏览器预览，提供 Linux 单机部署配置。

**Architecture:** FastAPI 同源托管现有静态前端；SQLite 持久化站点/素材/任务状态；Playwright 隔离每个站点浏览器上下文，通过面板远程画面完成手动登录；下载器将文件原子落盘。后台任务在页面关闭后继续运行，重启将中断任务标记可恢复。现有 Sites 地址继续是独立演示，真实模式由本地后端能力接口显式启用。

**Tech Stack:** Python 3.12, uv, FastAPI, SQLite, Playwright Chromium, httpx, yt-dlp, FFmpeg; vanilla JS frontend.

## Module contracts

- `backend/config.py` Settings(data_dir, allow_private_hosts, admin_token, max_download_bytes) and outbound URL checks.
- `backend/store.py` Store(path): `read()` returns a deep copy of UI-compatible state; `mutate(fn)` atomically updates SQLite JSON state and returns callback result. No demo assets in real state. IDs server-generated. Secrets stored outside public state.
- `backend/browser.py` BrowserService(data_dir, policy): async start/close, open_session(site), session_info(site_id), screenshot(site_id), interact(site_id, action), save_session(site_id), disconnect(site_id), context_for(site_id). Sessions encrypted on disk, never returned as cookies to UI. Isolated contexts; no automatic login, CAPTCHA, payment or consent clicks.
- `backend/scanner.py` Scanner(browser, policy): async `scan(site, options, on_asset, on_progress, cancelled)` emits normalized media dicts. Same-host page traversal only; external CDN media accepted after URL policy. Query/fragment navigation preserved where needed. Bounded depth/pages/assets/time. DOM imgs/srcset/video/source/poster/background + observed media response URLs. Existing session reused. DRM flagged unsupported, inaccessible/locked content not force-unlocked.
- `backend/downloader.py` Downloader(data_dir, browser, policy): async `download(asset, control, on_progress)` returns {path,size,sha256,mime,width,height,duration}; control allows pause/cancel. HTTP streaming direct resources; yt-dlp for HLS/DASH/media pages with domain-scoped cookies, time/size bounds and subprocess cancellation. Never treat HTML login pages as successful media. Persist completed only after output exists and validates.
- `backend/app.py` FastAPI endpoints and task orchestration. Serves frontend + downloads by ID, never arbitrary paths. `.framekeep` excluded from Git and static paths.
- `dist/real-api.js` generic client; `dist/real-session.js/css` remote auth window; `dist/app.js` real-mode branches with server state polling, no simulated timers or unlock actions in real mode.

## HTTP contract

GET `/api/capabilities` -> {mode:'real',version:1}; GET `/api/state` -> UI state including `mode:'real',scans:[]`.
POST `/api/sites` {name,url}; POST `/api/sites/{id}/session` opens isolated browser; GET session -> {open,url,title,saved,verified,tabs:[{id,title,url}],width:1280,height:800}; GET session/screenshot -> JPEG; POST session/input {type:'click'|'key'|'text'|'scroll'|'navigate'|'tab',x,y,key,text,deltaY,url,tabId}; POST session/save -> saved session metadata; DELETE session -> clears stored auth.
POST `/api/scans` {siteId?,url,depth,limit,kinds:['image','video']} -> scan; POST `/api/scans/{id}/cancel`.
POST `/api/downloads` {ids:[assetId]} -> {added,blocked,duplicates}; POST `/api/jobs/{id}/action` {action:'pause'|'resume'|'retry'|'cancel'}; POST `/api/jobs/pause`.
PATCH `/api/assets/{id}` {favorite?,tags?}; GET `/api/assets/{id}/file` actual media; GET `/api/assets/{id}/thumbnail` local preview cache when available.
PATCH `/api/settings` {concurrency}; real mode has no payment/unlock mutation, no watermark or scheduled collection execution.

## Tasks and verification

- [ ] 1. Write backend store/API/URL-policy tests; implement empty real state, transactional persistence and app lifecycle. Validate mutation survives a new Store instance and invalid URLs are rejected.
- [ ] 2. Write fixture-driven session and scanner tests. Implement user-controlled remote browser and encrypted auth state; prove authenticated fixture media is discovered only after user login, with bounded traversal and deduplication.
- [ ] 3. Write downloader tests for successful real bytes, HTTP errors/login HTML, pause/cancel and output validation. Implement HTTP/yt-dlp dispatch and persistence. Verify SHA-256/file size equals served fixture media.
- [ ] 4. Integrate existing UI through real-mode branches; browser-check site creation, remote login/save, scan, select download and file preview. Keep processing disabled with explanatory copy.
- [ ] 5. Add Dockerfile/Compose and exact local startup commands. Reuse Python lockfile; run real backend locally on loopback with persistent ignored data directory.
- [ ] 6. Run `uv run pytest`, `uv run ruff check backend tests_backend`, `npm test`, `npm run check`; inspect browser flow and live health. Perform chunlou.tv public/authorized-session smoke separately from controlled-fixture proof; never call site-specific login/download verified without actual evidence.

## Acceptance boundary

Real downloaded files and authenticated fixture verification demonstrate the implemented engine. chunlou.tv login requires the user to operate their own account in the panel; membership/paid episode availability is never inferred from saved cookies. Site-wide completeness, payments, watermark processing, multi-user hosting and background schedules are outside this round.

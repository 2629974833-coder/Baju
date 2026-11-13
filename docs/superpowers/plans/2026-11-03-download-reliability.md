# Download Reliability Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development and test-driven-development. Root reviews specification compliance, then code quality. Existing branch and uncommitted work stay intact; no automatic commits or destructive Git operations.

**Goal:** 实现可验证的断点续传、长视频与批量下载稳定性，再接入带依据和人工修正的正片/预览识别，并逐项验收。

**Architecture:** 延续 FastAPI / SQLite 单进程队列。下载器持久化私有续传检查点，HTTP 按验证器及 Range 恢复；HLS 重新确认清单后复用已校验片段。传输重试有次数/等待上限；队列隔离任务失败、禁止同素材并发写入、节流持久化进度。分类保存依据及置信度，未知保持未知，不将短视频自动视为预览。

**Tech Stack:** Python 3.12, asyncio, httpx, SQLite, Playwright, FFmpeg, yt-dlp, vanilla JS, pytest and node:test.

## Constraints and acceptance

- Work in existing `feat/real-media-pipeline`; baseline source snapshot at `test-output/download-module-baseline-20261103` excludes credentials/media. Main service 4173 stays running until changes pass checks and current saved session/data are backed up.
- Preserve existing URL/DNS/redirect/cookie boundaries and local-only external media tools. Do not expand DRM, paywall, login, purchase, site-wide crawling, or GPU scope.
- Current disk free space is about 11 GiB. Generated acceptance artifacts must peak below 1 GiB; use a 60-minute low-bitrate video and a separate 256 MiB valid media transfer, not multi-GiB downloads.
- Real Lustflix evidence already includes saved/restored account and free `/#/watch/C04/1` (31.2 s). Reuse only authorized content for targeted live regression. Controlled long-media results do not certify all target sites or membership access.

## Interfaces

- Keep `Downloader.download(asset, control, on_progress)` and existing result keys. Extra progress may include `phase`, `resumedBytes`, `retryCount`, `retryInSeconds`, `resumeSupported`, `resumeMessage`, `completedSegments`, `totalSegments`. Never include credential values, signed URLs, or filesystem checkpoint paths in public progress.
- Checkpoints are versioned, atomically written, private files. Hash source identity; store validators, validated byte offsets and integrity hashes. Keep original verified files intact on pause/cancel/failure. Cancel removes temporary state; pause/process shutdown preserves recoverable state.
- HTTP resume uses strong ETag, or a defensible strong Last-Modified validator, `Range` and `If-Range`; validate 206 offset/total/span. A 200 fallback restarts safely; changed/absent validators or invalid ranges never append blindly. 416 cannot turn arbitrary partial files into success.
- HLS re-fetches manifests, binds cached objects to manifest identity and resource identity, validates retained file hashes, and reuses only safe complete fragments. A partial segment can restart if range resume is unavailable. Keys/manifests may be re-fetched. Preserve strict VOD, protocol, resource, size and encryption checks.
- Retry only transient connection/HTTP 408/429/5xx failures with bounded exponential delay and capped Retry-After. Authentication, HTML, invalid media and unsupported protocols do not get infinite retries. Pause/cancel remain responsive during backoff and subprocess work.
- Classification object: `{kind: 'preview'|'episode'|'audio'|'unknown', confidence: 'high'|'medium'|'low', reasons: string[], source: 'automatic'|'manual', version: 1}`. Explicit preview markers outrank episode-page hints; duration alone never proves either class. Unknown is a valid output. Manual classification must survive rescans; reset returns to automatic.

## Task 1 — Durable transfer engine

Files: `backend/downloader.py`, focused new transport/checkpoint helper if needed, `tests_backend/test_download_resume.py`, existing downloader tests.

- [x] Add failing real HTTP fixture tests: paused bytes + new Downloader instance resumes with exact Range/If-Range; final bytes/SHA equal original; ignored Range restarts; changed/weak validator and malformed 206/416 cannot concatenate mixed versions; corrupted checkpoint/prefix is rejected safely.
- [x] Add failing HLS fixture tests: interruption after complete fragments, new instance resumes without their payload transfer; modified manifest/cache corruption invalidates reuse; separate audio and AES-128 remain valid.
- [x] Implement minimum checkpoint/resume/retry/progress behavior, with bounded streaming and cleanup on explicit cancel. Raise intelligible typed errors or sanitized ValueErrors.
- [x] Validate pause/cancel during network backoff, transient retries, no repeated 401/403, size/disk failures, process cleanup and generated long media.

Test pattern (using fixture-owned media and request log):

```python
with pytest.raises(DownloadPaused):
    await first.download(media_asset, pause_after_chunks, collect)
result = await Downloader(data_dir, browser, policy).download(media_asset, control(), collect)
assert any(headers.get('Range') == f'bytes={saved_offset}-' for headers in request_headers)
assert (data_dir / result['path']).read_bytes() == served_bytes
assert result['sha256'] == hashlib.sha256(served_bytes).hexdigest()
```

Run: `uv run pytest tests_backend/test_download_resume.py tests_backend/test_downloader.py -q`, then Ruff on changed files. Observe expected red tests before implementation.

## Task 2 — Queue and batch reliability

Files: `backend/tasks.py`, `backend/store.py`, `tests_backend/test_task_controls.py`, `tests_backend/test_download_queue.py`.

- [x] Write failing tests for one-writer-per-asset across retry/enqueue, immediately cancelled/paused scheduled jobs, failing browser startup, concurrency bounds, mixed failures in a batch, shutdown/restart with retained counters.
- [x] Preserve queued work across restart, convert interrupted running work to resumable paused state, keep completed files valid. Resume/retry must retain meaningful checkpoint progress until downloader confirms fallback/reset.
- [x] Throttle ordinary progress writes while persisting stage/control/terminal transitions immediately. Surface resume and retry events; do not add a second uncontrolled job retry loop.
- [x] Run realistic 50–100 job fixture batch at configured concurrency, with transient failures, fatal failures and pause/resume. All jobs reach an explicit terminal/paused state and other jobs continue.

Run: `uv run pytest tests_backend/test_task_controls.py tests_backend/test_download_queue.py -q`.

## Task 3 — Long-video and large-file acceptance

Files: new controlled acceptance runner/fixtures in `tests_backend/` or `scripts/`, results in ignored `test-output/`.

- [x] Generate valid 60-minute low-bitrate audio/video with FFmpeg; download HTTP and/or staged HLS through actual Downloader, verify expected duration and tracks, SHA/byte equality where applicable and complete decode.
- [x] Serve a valid media file >=256 MiB using streaming reads; interrupt and resume without repeating prefix payload, compare final bytes/hash, collect bounded memory evidence and elapsed time.
- [x] Include disconnection, stalled reads, server ignoring Range, manifest changes and retry exhaustion in tests; keep fixtures local and isolate main workspace.
- [x] Record actual sizes/durations/network bytes/reused bytes/test environment and all limitations; clean disposable bulk files after retaining sanitized evidence.

## Task 4 — Evidence-based content classification

Files: `backend/classification.py`, `backend/scanner.py`, `backend/tasks.py`, `backend/app.py`, focused backend tests; `dist/real-api.js`, `dist/app.js`, appropriate view helpers and frontend tests.

- [x] Failing tests: explicit preview/trailer paths, a legitimate short episode, generic long unknown, standalone audio rendition, conflicting evidence, manual override and rescan preservation.
- [x] Extract only necessary visible page/element signals and known URL patterns; produce sanitized reasons. Treat episode candidates as inference unless explicit evidence is available; avoid classifying all media on an episode page as episode footage.
- [x] Add validated per-asset manual classification/reset API and UI badge/detail/filter controls; preserve static demo mode.
- [x] Verify old assets receive automatic classification without redownloading; preview and the actual Lustflix Episode 1 classify differently while unknowns remain reviewable.

## Task 5 — Integration and final acceptance

- [x] Spec review per task, then code quality review and scoped code-simplifier pass. Resolve findings before final checks.
- [x] Run `uv run pytest tests_backend -q`, `uv run ruff check backend tests_backend`, `npm test`, `npm run check`, `git diff --check`.
- [x] Use isolated app instance for UI/API batch controls and classification correction. Use CUA for actual UI interaction. Preserve main account and originals.
- [x] After checks, back up main workspace/session privately, restart 4173 with updated code, confirm both localhost/127 health, saved account restoration, old files and sample playback.
- [x] Targeted live Lustflix resume/identification regression without purchases or unrelated downloads; keep controlled long/batch evidence distinct from live-site evidence.
- [x] Update `docs/DOWNLOAD_ACCEPTANCE.md`, QA, deployment configuration and runbook. Final report itemizes verified capabilities and remaining limits rather than claiming universal support.

## Protocol references

- HTTP validators and ranges: https://www.rfc-editor.org/rfc/rfc9110.html
- HLS playlist mutability and VOD: https://www.rfc-editor.org/rfc/rfc8216.html

## Acceptance note

Controlled-source resume passed, including actual SIGKILL recovery. Final Lustflix recovery reused 1,542,728 verified bytes across two complete fragments after adding safe 200-response handling for identical strong ETags. Full output matched the prior verified episode and decoded successfully. Network in-flight bytes are not claimed to be zero. See docs/DOWNLOAD_ACCEPTANCE.md for evidence and scope.

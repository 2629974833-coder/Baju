# Download quality and audio selection implementation plan

> For agentic workers: use subagent-driven-development with test-first implementation and independent review. Preserve all existing uncommitted changes; no automatic commits.

**Goal:** Let users inspect actual HLS/DASH options, choose a maximum source resolution and audio language for single/batch/episode downloads, and verify the selected output.

**Architecture:** Keep the existing downloader/network/cache/queue. Store immutable `job.downloadOptions`, attach it privately to each transfer (including after refreshed URLs), and store public actual `selection` results. Reuse yt-dlp MPD metadata and ISO639 normalization; no transcoding, GPU or new format-conversion subsystem.

**Tech stack:** Existing FastAPI/Pydantic, SQLite, Playwright, yt-dlp, FFmpeg, plain JavaScript.

## Shared contracts

`backend.selection.normalize_selection(value=None)` returns `{maxHeight: null|int144..4320, audioLanguage:'auto'|language-tag}` and rejects invalid types/extra fields. Language primary is 2–3 ASCII letters, subtags 2–8 alphanumeric characters, max35; normalize common ISO639 aliases. Explicit regional tags never silently select another specified region. A base language may select a matching regional default. Missing cap/language evidence is an error for a nondefault adaptive selection; defaults retain prior compatibility.

`Downloader.describe_options(asset)` returns only `{protocol:'http'|'hls'|'dash',selectable:bool,heights:[int],audioTracks:[{language,name,default}],message:str}`. Fetch root manifests via existing policy, headers/cookies and size budgets, no media fragments, temporary files always cleaned. Root API limits concurrent inspections to2 with20sec timeout. No sourceURLs, credentials or signed parameters in option metadata/errors.

`Downloader.download` receives `asset._downloadOptions`, returns/updates `selection:{protocol,height,audioLanguage,optionsApplied,message?}`. Direct files/images preserve source bytes; optionsApplied=false, explained in UI. Chosen representations and options must bind checkpoints. Source refresh retains the same job choices. Original downloaded files stay protected from replacement by new queue choices.

## Task 1: engine and format inspection (engine agent)

Files: backend/selection.py, backend/dash.py, backend/downloader.py, tests_backend/test_download_selection.py and focused DASH tests.

- [x] RED: actual multiple-resolution/multiple-language HLS/DASH fixtures, requested ceiling/language, no-match/unknown evidence, ISO639 aliases/region mismatch, inspect without fragments, changed selection after pause.
- [x] GREEN: bounded metadata inspection, shared selectors, actual-selection reporting and cache identity using the existing downloader/SDK.
- [x] Verify actual ffprobe dimensions/tracks, full decode and protocol/cache regressions. Review source and defaults for compatibility.

## Task 2: API and task persistence (root)

Files: backend/app.py, backend/tasks.py, tests_backend/test_selection_queue_api.py.

- [x] RED: POST downloads/options and selected-episode/options validation, immutable persisted choices, recovery retains choices, retry may replace choices only for failed/needs-user/cancelled jobs, old jobs use defaults.
- [x] GREEN: add DownloadOptionsInput, optional options on downloads/episode-download/retry. Add GET /api/assets/{id}/download-options with safe bounded inspection.
- [x] Persist result.selection on job and asset.downloadSelection; selection changes participate in immediate progress updates. Keep per-asset writer protection and original files unchanged.
- [x] Verify restart with new Store/TaskManager and refreshed source retains choices, malicious/unsupported inputs rejected, no private fields returned.

## Task 3: real panel flow (UI agent)

Files: dist/download-options-view.js, dist/app.js, dist/workflow-view.js, added CSS and tests/download-options-view.test.mjs; package syntax-check list by root.

- [x] RED: source lists, unknown options, failed loading with explicit retry/default, batch form serialization, language validation, queue/detail actual-vs-requested display and escaped metadata.
- [x] GREEN: single video opens inspected options modal, batch and episode batches choose once, images can retain direct flow; failed jobs can change options then retry. No added dependency or prototype-only choices.
- [x] Verify tests, syntax and real browser selection/queue result. Preserve existing successful toasts and episode blocked/duplicate counts.

## Task 4: integration and rollout (root plus independent reviewer)

- [x] Source snapshot test-output/download-selection-baseline-20261103 excludes credentials/media. Review spec first then code clarity; apply small code-simplifier cleanup.
- [x] Run full backend/frontend/Ruff/syntax/diff checks and controlled mixed-video acceptance as warranted by download changes.
- [x] Use only saved Lustflix account. Inspect known first-episode master and choose a real available lower resolution in an isolated workspace; verify ffprobe, complete decode and original main hashes unchanged. Do not purchase or access other accounts. Other four episodes remain permission-limited.
- [x] Back up main SQLite/encrypted session, restart4173 after checks; verify localhost/127 health, preserved originals/session, options UI and actual behavior. Record measured evidence and remaining boundaries in docs/DOWNLOAD_SELECTION.md and QA.

Docker storage is still insufficient from prior evidence; do not prune user images/volumes or repeatedly rebuild without a change that addresses the blocker. No watermark or super-resolution scope.

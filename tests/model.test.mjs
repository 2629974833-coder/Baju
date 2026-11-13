import test from "node:test";
import assert from "node:assert/strict";
import {
  createInitialState,
  enqueueDownloads,
  startProcessing,
  unlockAssets,
  setSiteAuth,
  transitionJob,
  tickJobs,
  normalizeSiteUrl,
} from "../dist/model.js";

const asset = (state, id) => state.assets.find((item) => item.id === id);
const freeze = (value) => {
  if (value && typeof value === "object") {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
};
const cleanState = () => {
  const state = createInitialState();
  state.jobs = [];
  return state;
};

test("seed provides a complete demo library and independent state instances", () => {
  const state = createInitialState();
  assert.equal(state.version, 1);
  assert.equal(state.assets.length, 16);
  assert.equal(state.assets.filter((item) => item.locked).length, 3);
  assert.ok(
    state.assets.filter((item) => item.status === "downloaded").length >= 4,
  );
  assert.ok(
    state.assets.filter((item) => item.status === "processed").length >= 2,
  );
  assert.deepEqual(
    new Set(state.jobs.map((item) => item.status)),
    new Set(["running", "paused", "failed", "completed"]),
  );
  state.assets[0].tags.push("changed");
  assert.ok(!createInitialState().assets[0].tags.includes("changed"));
});

test("demo presets and rule values match the supported watermark-only controls", () => {
  const state = createInitialState();
  const schedules = new Set(["每天 09:00", "每6小时", "每周一09:00"]);
  const actions = new Set(["下载新增素材", "扫描并等待挑选"]);
  assert.ok(
    state.rules.every(
      (rule) => schedules.has(rule.schedule) && actions.has(rule.action),
    ),
  );
  assert.ok(
    state.assets
      .flatMap((item) => item.versions)
      .every((version) => version.preset === "品牌水印 · 右下角"),
  );
  const ready = state.assets.find((item) => item.status === "downloaded");
  const result = startProcessing(state, [ready.id]);
  assert.equal(result.state.jobs.at(-1).preset, "品牌水印 · 右下角");
});

test("download queue blocks locked and expired sources while deduplicating downloads", () => {
  const state = cleanState();
  const available = state.assets.find(
    (item) =>
      item.status === "discovered" && !item.locked && item.siteId === "s2",
  );
  const locked = state.assets.find((item) => item.locked);
  const expired = state.assets.find(
    (item) => item.status === "discovered" && item.siteId === "s3",
  );
  const downloaded = state.assets.find((item) => item.status === "downloaded");
  const before = structuredClone(state);
  const result = enqueueDownloads(freeze(state), [
    available.id,
    available.id,
    locked.id,
    expired.id,
    downloaded.id,
  ]);
  assert.equal(result.added, 1);
  assert.equal(result.blocked, 2);
  assert.equal(result.duplicates, 2);
  assert.equal(result.state.jobs.length, 1);
  assert.equal(result.state.jobs[0].status, "queued");
  assert.deepEqual(state, before);
  assert.notEqual(result.state.assets, state.assets);
  assert.equal(enqueueDownloads(result.state, [available.id]).duplicates, 1);
});

test("only connected sources may simulate unlocking and each unlock is recorded", () => {
  const state = cleanState();
  const locked = state.assets.filter((item) => item.locked);
  const updated = unlockAssets(
    freeze(state),
    locked.map((item) => item.id),
  );
  assert.ok(locked.every((item) => !asset(updated, item.id).locked));
  assert.equal(updated.activity.length, state.activity.length + 1);
  assert.ok(locked.every((item) => item.locked));
  const disconnected = setSiteAuth(state, "s1", "disconnected");
  assert.ok(
    unlockAssets(disconnected, [locked[0].id]).assets.find(
      (item) => item.id === locked[0].id,
    ).locked,
  );
  assert.equal(state.sites.find((item) => item.id === "s1").auth, "connected");
  assert.equal(
    setSiteAuth(state, "s1", "invalid").sites.find((item) => item.id === "s1")
      .auth,
    "connected",
  );
});

test("processing accepts local files independent of source login and blocks remote-only assets", () => {
  const state = cleanState();
  const ready = state.assets.find((item) => item.status === "downloaded");
  const remote = state.assets.find((item) => item.status === "discovered");
  state.sites.find((item) => item.id === ready.siteId).auth = "expired";
  const result = startProcessing(
    freeze(state),
    [ready.id, remote.id, ready.id],
    "品牌水印",
  );
  assert.equal(result.added, 1);
  assert.equal(result.blocked, 1);
  assert.equal(result.duplicates, 1);
  assert.equal(result.state.jobs[0].preset, "品牌水印");
  assert.equal(
    startProcessing(result.state, [ready.id], "文字水印").duplicates,
    1,
  );
});

test("pause preserves progress, resume queues work, cancel removes work, and retry resets failures", () => {
  const state = createInitialState();
  const running = state.jobs.find((item) => item.status === "running");
  const failed = state.jobs.find((item) => item.status === "failed");
  const paused = transitionJob(freeze(state), running.id, "pause");
  assert.equal(
    paused.jobs.find((item) => item.id === running.id).status,
    "paused",
  );
  assert.equal(
    paused.jobs.find((item) => item.id === running.id).progress,
    running.progress,
  );
  const resumed = transitionJob(paused, running.id, "resume");
  assert.equal(
    resumed.jobs.find((item) => item.id === running.id).status,
    "queued",
  );
  const retried = transitionJob(
    setSiteAuth(state, "s3", "connected"),
    failed.id,
    "retry",
  );
  assert.equal(
    retried.jobs.find((item) => item.id === failed.id).status,
    "queued",
  );
  assert.equal(retried.jobs.find((item) => item.id === failed.id).progress, 0);
  assert.equal(
    retried.jobs.find((item) => item.id === failed.id).error,
    undefined,
  );
  assert.ok(
    !transitionJob(state, running.id, "cancel").jobs.some(
      (item) => item.id === running.id,
    ),
  );
  assert.equal(
    state.jobs.find((item) => item.id === running.id).status,
    "running",
  );
});

test("workers obey separate download and processing limits and preserve paused work", () => {
  let state = cleanState();
  state.settings.concurrency = 2;
  state.sites.forEach((item) => {
    item.auth = "connected";
  });
  state.assets.forEach((item) => {
    item.locked = false;
  });
  const remote = state.assets.filter((item) => item.status === "discovered");
  const local = state.assets.filter((item) => item.status === "downloaded");
  state = enqueueDownloads(
    state,
    remote.map((item) => item.id),
  ).state;
  state = startProcessing(
    state,
    local.map((item) => item.id),
    "文字水印",
  ).state;
  const pausedId = state.jobs[0].id;
  state = transitionJob(state, pausedId, "pause");
  const next = tickJobs(freeze(state));
  assert.equal(
    next.jobs.filter(
      (item) => item.type === "download" && item.status === "running",
    ).length,
    2,
  );
  assert.equal(
    next.jobs.filter(
      (item) => item.type === "process" && item.status === "running",
    ).length,
    1,
  );
  assert.equal(next.jobs.find((item) => item.id === pausedId).progress, 0);
  assert.equal(next.jobs.find((item) => item.id === pausedId).status, "paused");
  assert.ok(
    next.jobs
      .filter((item) => item.status === "running")
      .every((item) => item.progress > 0),
  );
});

test("completed work updates assets, retains originals, and adds exactly one output version", () => {
  let state = cleanState();
  const remote = state.assets.find(
    (item) =>
      item.status === "discovered" && !item.locked && item.siteId === "s2",
  );
  state = enqueueDownloads(state, [remote.id]).state;
  for (let i = 0; i < 30; i++) state = tickJobs(state);
  assert.equal(asset(state, remote.id).status, "downloaded");
  assert.equal(state.jobs[0].status, "completed");
  assert.equal(state.jobs[0].progress, 100);
  const original = structuredClone(asset(state, remote.id));
  state = startProcessing(state, [remote.id], {
    name: "品牌水印",
    watermark: true,
  }).state;
  for (let i = 0; i < 30; i++) state = tickJobs(state);
  const output = asset(state, remote.id);
  assert.equal(output.status, "processed");
  assert.equal(output.versions.length, original.versions.length + 1);
  assert.equal(output.thumbnail, original.thumbnail);
  assert.equal(output.width, original.width);
  assert.equal(output.sizeMB, original.sizeMB);
  const completed = tickJobs(state);
  assert.equal(
    asset(completed, remote.id).versions.length,
    output.versions.length,
  );
});

test("download workers fail clearly if authorization expires while waiting", () => {
  let state = cleanState();
  const target = state.assets.find(
    (item) =>
      item.status === "discovered" && !item.locked && item.siteId === "s1",
  );
  state = enqueueDownloads(state, [target.id]).state;
  state = setSiteAuth(state, "s1", "expired");
  const next = tickJobs(state);
  assert.equal(next.jobs[0].status, "failed");
  assert.ok(next.jobs[0].error);
  assert.equal(asset(next, target.id).status, "discovered");
});

test("automatic processing queues once after a download when enabled", () => {
  let state = cleanState();
  state.settings.autoProcess = true;
  const target = state.assets.find(
    (item) =>
      item.status === "discovered" && !item.locked && item.siteId === "s2",
  );
  state = enqueueDownloads(state, [target.id]).state;
  for (let i = 0; i < 40; i++) state = tickJobs(state);
  assert.equal(state.jobs.filter((item) => item.type === "process").length, 1);
  assert.equal(
    state.jobs.find((item) => item.type === "process").preset,
    "品牌水印 · 右下角",
  );
  assert.equal(asset(state, target.id).status, "processed");
});

test("site URLs normalize HTTP addresses and reject scripts, credentials and invalid input", () => {
  assert.equal(
    normalizeSiteUrl("  pexels.com/videos  "),
    "https://pexels.com/videos",
  );
  assert.equal(
    normalizeSiteUrl("HTTPS://EXAMPLE.COM/path#section"),
    "https://example.com/path",
  );
  assert.equal(normalizeSiteUrl("http://example.com"), "http://example.com/");
  for (const input of [
    "",
    null,
    "javascript:alert(1)",
    "data:text/html,test",
    "ftp://example.com",
    "https://user:pass@example.com",
    "not a website",
    "https://",
  ]) {
    assert.equal(normalizeSiteUrl(input), null, String(input));
  }
});

test('scan URLs preserve SPA episode routes but discard in-page anchors', async () => {
  const { normalizeScanUrl } = await import('../dist/model.js');
  assert.equal(normalizeScanUrl('https://lustflix.tv/#/watch/C04/1'), 'https://lustflix.tv/#/watch/C04/1');
  assert.equal(normalizeScanUrl('https://example.com/page#section'), 'https://example.com/page');
});

import test from "node:test";
import assert from "node:assert/strict";
import { createInitialState } from "../dist/model.js";
import {
  isValidStoredState,
  getVersionPreview,
  getExportAssets,
} from "../dist/view-model.js";

test("storage validation accepts current seed and complete empty collections", () => {
  assert.equal(isValidStoredState(createInitialState()), true);
  const empty = createInitialState();
  for (const key of ["assets", "sites", "jobs", "rules", "activity"])
    empty[key] = [];
  assert.equal(isValidStoredState(empty), true);
});

test("storage validation rejects incomplete or invalid top-level values without throwing", () => {
  for (const value of [
    null,
    undefined,
    1,
    "state",
    [],
    {},
    { version: 1, assets: [], jobs: [] },
  ]) {
    assert.equal(isValidStoredState(value), false);
  }
  for (const field of [
    "assets",
    "sites",
    "jobs",
    "rules",
    "activity",
    "settings",
  ]) {
    const state = createInitialState();
    delete state[field];
    assert.equal(isValidStoredState(state), false, field);
  }
});

test("storage validation checks rendering fields and nested versions", () => {
  const mutations = [
    (state) => {
      state.assets[0].tags = ["valid", 7];
    },
    (state) => {
      state.assets[0].thumbnail = null;
    },
    (state) => {
      state.assets[0].kind = "unknown";
    },
    (state) => {
      state.assets[0].width = Infinity;
    },
    (state) => {
      state.assets[0].locked = "false";
    },
    (state) => {
      state.assets[0].versions = [null];
    },
    (state) => {
      state.assets[8].versions[0].preset = [];
    },
    (state) => {
      state.assets[8].versions[0].label = 42;
    },
    (state) => {
      state.sites[0].auth = "invalid";
    },
    (state) => {
      state.sites[0].name = null;
    },
    (state) => {
      state.jobs[0].status = "invalid";
    },
    (state) => {
      state.jobs[0].progress = NaN;
    },
    (state) => {
      state.rules[0].enabled = "yes";
    },
    (state) => {
      state.rules[0].schedule = null;
    },
    (state) => {
      state.activity[0].message = {};
    },
    (state) => {
      state.activity[0].createdAt = "not a date";
    },
    (state) => {
      state.settings.concurrency = Infinity;
    },
    (state) => {
      state.settings.autoProcess = "false";
    },
  ];
  for (const mutate of mutations) {
    const state = createInitialState();
    mutate(state);
    assert.equal(isValidStoredState(state), false, String(mutate));
  }
});

test("remove-only previews never add a watermark", () => {
  assert.equal(
    getVersionPreview({ preset: { mode: "remove", text: "EXAMPLE" } })
      .showWatermark,
    false,
  );
});

test("watermark previews preserve text, position, and percentage opacity", () => {
  assert.deepEqual(
    getVersionPreview({
      preset: { mode: "both", text: "拾帧", position: "top-left", opacity: 25 },
    }),
    {
      showWatermark: true,
      text: "拾帧",
      position: "top-left",
      opacity: 25,
    },
  );
  assert.equal(
    getVersionPreview({ preset: { mode: "add", opacity: 0 } }).opacity,
    0,
  );
});

test("legacy string presets and malformed preview fields receive safe defaults", () => {
  const defaults = {
    showWatermark: true,
    text: "FRAMEKEEP",
    position: "bottom-right",
    opacity: 65,
  };
  assert.deepEqual(
    getVersionPreview({ preset: "品牌水印 · 右下角" }),
    defaults,
  );
  assert.deepEqual(getVersionPreview(null), defaults);
  assert.deepEqual(
    getVersionPreview({
      preset: { text: {}, position: "center", opacity: NaN },
    }),
    defaults,
  );
  assert.equal(getVersionPreview({ preset: { opacity: 200 } }).opacity, 100);
  assert.equal(getVersionPreview({ preset: { opacity: -5 } }).opacity, 0);
});

test("library export contains only finished assets while other pages export all", () => {
  const state = createInitialState();
  assert.deepEqual(
    getExportAssets(state, "library"),
    state.assets.filter((asset) => asset.status === "processed"),
  );
  assert.equal(getExportAssets(state, "library").length, 2);
  assert.equal(getExportAssets(state, "settings").length, 16);
});

test("explicit export selection takes priority including an empty selection", () => {
  const state = createInitialState();
  assert.deepEqual(getExportAssets(state, "library", ["a1", "a9", "missing"]), [
    state.assets[0],
    state.assets[8],
  ]);
  assert.deepEqual(getExportAssets(state, "settings", []), []);
  const exported = getExportAssets(state, "settings");
  exported.pop();
  assert.equal(state.assets.length, 16);
});

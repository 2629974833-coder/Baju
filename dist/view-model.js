const isRecord = (value) =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const isText = (value) => typeof value === "string";
const isName = (value) => isText(value) && value.trim().length > 0;
const isFiniteNumber = (value) =>
  typeof value === "number" && Number.isFinite(value);
const isBoolean = (value) => typeof value === "boolean";
const isDate = (value) => isName(value) && Number.isFinite(Date.parse(value));
const names = (value, keys) => keys.every((key) => isName(value[key]));
const optional = (value, key, validate) =>
  value[key] === undefined || validate(value[key]);
const collection = (value, validate) =>
  Array.isArray(value) && value.every(validate);
const positions = ["top-left", "top-right", "bottom-left", "bottom-right"];

function validPreset(value) {
  if (isText(value)) return true;
  return (
    isRecord(value) &&
    optional(value, "mode", (item) =>
      ["add", "remove", "both"].includes(item),
    ) &&
    optional(value, "text", isText) &&
    optional(value, "name", isText) &&
    optional(value, "label", isText) &&
    optional(value, "position", isText) &&
    optional(value, "opacity", isFiniteNumber)
  );
}

function validVersion(value) {
  return (
    isRecord(value) &&
    isName(value.id) &&
    isDate(value.createdAt) &&
    validPreset(value.preset) &&
    optional(value, "name", isText) &&
    optional(value, "label", isText) &&
    optional(value, "thumbnail", isText) &&
    optional(value, "width", isFiniteNumber) &&
    optional(value, "height", isFiniteNumber) &&
    optional(value, "sizeMB", isFiniteNumber)
  );
}

function validAsset(value) {
  return (
    isRecord(value) &&
    names(value, ["id", "title", "category", "siteId", "thumbnail"]) &&
    ["video", "image"].includes(value.kind) &&
    ["discovered", "downloaded", "processed"].includes(value.status) &&
    isText(value.duration) &&
    isDate(value.date) &&
    ["width", "height"].every(
      (key) => isFiniteNumber(value[key]) && value[key] > 0,
    ) &&
    isFiniteNumber(value.sizeMB) &&
    value.sizeMB >= 0 &&
    isBoolean(value.locked) &&
    isBoolean(value.favorite) &&
    isFiniteNumber(value.price) &&
    value.price >= 0 &&
    collection(value.tags, isText) &&
    collection(value.versions, validVersion)
  );
}

function validSite(value) {
  return (
    isRecord(value) &&
    names(value, ["id", "name", "domain", "initials", "access", "updated"]) &&
    ["connected", "expired", "public", "disconnected"].includes(value.auth) &&
    isFiniteNumber(value.count) &&
    value.count >= 0
  );
}

function validJob(value) {
  return (
    isRecord(value) &&
    names(value, ["id", "assetId"]) &&
    ["download", "process"].includes(value.type) &&
    ["running", "queued", "paused", "failed", "completed"].includes(
      value.status,
    ) &&
    isFiniteNumber(value.progress) &&
    value.progress >= 0 &&
    value.progress <= 100 &&
    isDate(value.createdAt) &&
    optional(value, "error", isText) &&
    optional(value, "preset", validPreset)
  );
}

function validRule(value) {
  return (
    isRecord(value) &&
    names(value, ["id", "name", "siteId", "schedule", "action"]) &&
    isBoolean(value.enabled)
  );
}

function validActivity(value) {
  return (
    isRecord(value) &&
    names(value, ["id", "message"]) &&
    isDate(value.createdAt) &&
    optional(value, "type", isText)
  );
}

export function isValidStoredState(value) {
  try {
    return (
      isRecord(value) &&
      value.version === 1 &&
      collection(value.assets, validAsset) &&
      collection(value.sites, validSite) &&
      collection(value.jobs, validJob) &&
      collection(value.rules, validRule) &&
      collection(value.activity, validActivity) &&
      isRecord(value.settings) &&
      Number.isInteger(value.settings.concurrency) &&
      value.settings.concurrency >= 1 &&
      isBoolean(value.settings.autoProcess) &&
      isBoolean(value.settings.preserveOriginal)
    );
  } catch {
    return false;
  }
}

export function getVersionPreview(version) {
  const preset = isRecord(version?.preset) ? version.preset : {};
  const opacity = isFiniteNumber(preset.opacity) ? preset.opacity : 65;
  return {
    showWatermark: preset.mode !== "remove",
    text: isName(preset.text) ? preset.text : "FRAMEKEEP",
    position: positions.includes(preset.position)
      ? preset.position
      : "bottom-right",
    opacity: Math.min(100, Math.max(0, opacity)),
  };
}

export function getExportAssets(state, page, ids) {
  if (ids !== undefined) {
    const selected = new Set(Array.isArray(ids) ? ids : []);
    return state.assets.filter((asset) => selected.has(asset.id));
  }
  if (page === "library")
    return state.assets.filter((asset) => asset.status === "processed");
  return [...state.assets];
}

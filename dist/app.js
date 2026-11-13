import { icon, escapeHtml as esc, formatSize } from "./ui.js";
import {
  isValidStoredState,
  getVersionPreview,
  getExportAssets,
} from "./view-model.js";
import {
  createInitialState,
  enqueueDownloads,
  startProcessing,
  unlockAssets,
  setSiteAuth,
  transitionJob,
  tickJobs,
  normalizeSiteUrl,
  normalizeScanUrl,
} from "./model.js?v=20261113-public-download-2";
import {
  renderSites,
  renderAuthModal,
  renderUnlockModal,
  renderAddSiteModal,
  isLataSite,
} from "./sites-view.js";
import {
  renderQueue,
  downloadCaption,
  renderProcess,
  renderAutomation,
  renderSettings,
  renderRuleModal,
} from "./workflow-view.js";

import { discoveryCatalog, discoveryScanIdentity, episodeStatus, filterEpisodeEntries, seriesCheckBody, renderEpisodeCatalog, selectCatalogEpisodes, catalogScanMessage, catalogDownloadMessage } from "./catalog-view.js";
import { episodeThumbnails, filterSeriesItems, seriesPageItems, SERIES_PAGE_SIZE, renderSeriesLibrary, seriesPoster, seriesStrip } from "./series-view.js";
import { downloadRequest, parseDownloadOptions, renderDownloadOptions, renderDownloadSelection } from "./download-options-view.js";
import { contentKinds, classificationLabel, matchesContent, renderClassification } from "./content-view.js";
import { changedRuleFields, ruleRequest } from "./automation-view.js";
import { createApiClient } from "./real-api.js";
import { createSessionPanel } from "./real-session.js";
import { bindMotionEditor, newMotionDraft } from "./motion-editor.js";
import { PROCESSING_DEFAULTS, renderRealProcess, renderProcessingJobs, processingRequest, studioBatch, glyphRequest, bindProcessingRegion, updateProcessingPreview } from "./processing-view.js";
import { groupLocalAssets } from "./asset-series.js";
import { LIBRARY_PAGE_SIZE, librarySearchIds, libraryVisibleGroups, renderLibraryGroups } from "./library-groups-view.js";
import { hasOriginal, matchesFileCollection, libraryFilePresentation, processedVersions, versionFileUrl, renderVersionWorkspace, renderVersionRecords } from "./version-view.js";

import {publicWorkspace, workspacePage, renderWorkspaceNotice, saveOriginalLink, renderPublicPreview} from "./public-workspace.js";

const api = createApiClient();
const STORAGE = "framekeep-prototype-v1";
import { availableForDownload, freshResultView, intakeScopes, renderIntakeResult, reservedAssets, resultItems, resultRoute, suggestScope } from "./intake-results.js?v=20261113-public-download-2";
import { intakeDestination, homePendingAssets, renderHomePending } from "./intake-workflow.js";

const pages = {
  home: "总览",
  results: "拾取结果",
  discover: "发现",
  series: "剧集",
  process: "工作室",
  library: "资料库",
  queue: "活动",
  sites: "来源",
  automation: "自动化",
  settings: "系统",
};
let state;
let serviceError = "", polling = false, stateReadGeneration = 0;
const isReal = () => state?.mode === "real";
function loadDemoState() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE));
    return isValidStoredState(saved) ? saved : createInitialState();
  } catch { return createInitialState(); }
}
const COIN_PREFS = "framekeep-series-coin-prefs-v1";
function loadCoinDrafts() {
  try {
    const saved = JSON.parse(localStorage.getItem(COIN_PREFS));
    return saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {};
  } catch { return {}; }
}
function updateCoinDraft(id, field, value) {
  ui.coinDrafts[id] = {...ui.coinDrafts[id], [field]:value};
  try { localStorage.setItem(COIN_PREFS, JSON.stringify(ui.coinDrafts)); } catch {
    if (!storageWarningShown) {
      storageWarningShown = true;
      toast("当前浏览器无法保存本剧设置，关闭页面后将恢复默认。", true);
    }
  }
}
const ui = {
  episodeSelection: {},
  seriesCheckSubmitting: false,
  coinDrafts: loadCoinDrafts(),
  discoverySelection: {},
  discoveryExpanded: {},
  discoveryGroupOpen: {},
  discoveryGroupPages: {},
  discoveryRecordsOpen: false,
  discoveryFilters: {},
  discoveryScanLimit: 3,
  discoveryPage: 1,
  discoveryPageSize: 24,
  discoveryFiltersOpen: false,
  discoveryGroup: "all",
  seriesExpanded: {},
  seriesScanOptionsOpen: false,
  submittedSeriesScanId: null,
  checkedSeries: [],
  seriesFilters: { query: "", siteId: "", category: "", page: 1 },
  seriesDownloadSubmitting: new Set(),
  homePending: {open:false, query:"", page:1, selected:[]},
  episodeFilters: {},
  seriesDraft: { url: "", siteId: "", limit: 100 },
  result: { id: resultRoute(location.hash), data: null, error: "", loading: false, view: freshResultView() },
  page: resultRoute(location.hash) ? "results" : location.hash === "#discover" ? "home" : Object.hasOwn(pages, location.hash.slice(1)) ? location.hash.slice(1) : "home",
  focusId: "",
  inspectorEnter: false,
  inbox: "pending",
  pageEnter: false,
  modalOpening: false,
  flashSeriesScanId: "",
  homeScope: "site",
  explicitHomeScope: false,
  homeDraft: "",
  homeBusy: false,
  quickDraft: "",
  selected: new Set(),
  kind: "all",
  site: "all",
  category: "all",
  content: "all",
  status: "all",
  q: "",
  sort: "recent",
  librarySort: "episode",
  librarySections: new Map(),
  view: "grid",
  collection: "all",
  queueFilter: "all",
  processIds: [],
  processingDraft: { groupId: "", incomingIds: [], tracks: new Map(), options: { ...PROCESSING_DEFAULTS, region: { ...PROCESSING_DEFAULTS.region } }, previewId: "" },
  processingBusy: false,
  archiveBusy: false,
  operations: { data: null, backups: [], loading: false, error: "", actionError: "", loadError: "", generation: 0, busy: false, updatedAt: 0 },
  preset: {
    mode: "add",
    text: "FRAMEKEEP",
    position: "bottom-right",
    opacity: 65,
    name: "品牌水印 · 右下角",
  },
  modal: null,
  scan: null,
};
let toastTimer,
  scanTimer,
  lastFocus,
  storageWarningShown = false;
const $ = (selector) => document.querySelector(selector);
const findAsset = (id) => state.assets.find((a) => a.id === id);
const findSite = (id) => state.sites.find((s) => s.id === id);
const uniqueId = (prefix) =>
  `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
function persist() {
  if (isReal() || !state) return;
  try {
    localStorage.setItem(STORAGE, JSON.stringify(state));
  } catch {
    if (!storageWarningShown) {
      storageWarningShown = true;
      toast("浏览器存储不可用，当前操作仅保留到本次页面关闭。", true);
    }
  }
}
function activity(message, type = "action") {
  state.activity.unshift({
    id: uniqueId("event"),
    type,
    message,
    createdAt: new Date().toISOString(),
  });
  state.activity = state.activity.slice(0, 100);
}
function toast(message, error = false, action = null) {
  clearTimeout(toastTimer);
  const root = $("#toast-root");
  if (!root) return;
  const link = action ? `<button class="toast-action" data-action="${esc(action.action || "navigate")}" ${action.page ? `data-page="${esc(action.page)}"` : ""}>${esc(action.label)}${icon("arrowRight", 12)}</button>` : "";
  root.innerHTML = `<div class="toast ${error ? "error" : ""}" role="${error ? "alert" : "status"}">${icon(error ? "alertCircle" : "checkCircle", 18)}<span class="toast-text">${esc(message)}</span>${link}<i class="toast-timer" style="animation-duration:${error ? 6000 : 4200}ms"></i></div>`;
  toastTimer = setTimeout(() => {
    const current = root.querySelector(".toast");
    if (!current) return;
    current.classList.add("is-leaving");
    setTimeout(() => { if (current.isConnected) root.innerHTML = ""; }, 220);
  }, error ? 6000 : 4200);
}
function statusLabel(asset) {
  if (asset.locked) return ["待解锁", "locked", "lock"];
  if (asset.status === "processed") return ["已处理", "", "check"];
  if (asset.status === "downloaded") return ["已下载", "", "check"];
  return null;
}
function shortSite(site) {
  return site?.name?.split(" ")[0] || "未知来源";
}
function pageHeading(title, subtitle, actions, eyebrow = "工作空间") {
  return `<header class="page-heading"><div class="page-heading-copy"><div class="eyebrow">${eyebrow}</div><h1>${title}</h1>${subtitle ? `<p>${subtitle}</p>` : ""}</div><div class="page-heading-actions">${actions || ""}</div></header>`;
}
function navItem(id, name, ico) {
  if (workspacePage(state, id) !== id) return "";
  return `<button class="nav-item ${ui.page === id ? "active" : ""}" data-action="navigate" data-page="${id}" ${ui.page === id ? 'aria-current="page"' : ""}>${icon(ico, 18)}<span class="nav-label">${name}</span><span class="nav-count" data-nav-count="${id}"></span></button>`;
}
const BRAND_MARK = `<svg class="brand-mark" viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" rx="16" fill="#191917"/><rect x=".75" y=".75" width="62.5" height="62.5" rx="15.25" fill="none" stroke="#fff" stroke-opacity=".1" stroke-width="1.5"/><path d="M17 26v-9h9M38 17h9v9M47 38v9h-9M26 47h-9v-9" fill="none" stroke="#F2EEE6" stroke-width="3.6" stroke-linecap="round" stroke-linejoin="round"/><circle cx="32" cy="32" r="5.5" fill="#6AD1A8"/></svg>`;
function renderShell() {
  const theme = document.documentElement.dataset.theme === "light" ? "light" : "dark";
  $("#app").innerHTML =
    `<div class="mobile-nav-backdrop" data-action="close-nav"></div><aside class="app-sidebar" id="workspace-navigation" aria-label="主导航"><button class="icon-button drawer-close" data-action="close-nav" aria-label="关闭导航">${icon("x",18)}</button><a class="brand" href="#home" aria-label="拾帧总览">${BRAND_MARK}<span class="brand-words"><strong>拾帧</strong><small>FRAMEKEEP</small></span></a><button class="intake-button" data-action="new-scan">${icon("plus", 17)}<span>拾取链接</span><kbd>N</kbd></button><div class="nav-caption">流程</div><nav class="nav-group" aria-label="工作流程">${navItem("home", pages.home, "overview")}${navItem("series", pages.series, "film")}${navItem("process", pages.process, "wand")}${navItem("library", pages.library, "archive")}</nav><div class="nav-caption">运行</div><nav class="nav-group" aria-label="运行与管理">${navItem("queue", pages.queue, "pulse")}${navItem("sites", pages.sites, "globe")}${navItem("automation", pages.automation, "zap")}${navItem("settings", pages.settings, "tune")}</nav><div class="sidebar-bottom"><div id="live-tray"></div><button class="nav-item" data-action="help">${icon("help", 17)}<span class="nav-label">使用指南</span></button></div></aside><main class="app-main"><header class="app-topbar"><button class="icon-button mobile-menu" aria-label="打开导航" data-action="open-nav" aria-controls="workspace-navigation" aria-expanded="false">${icon("menu", 19)}</button><div class="breadcrumb"><span class="breadcrumb-root">拾帧</span>${icon("chevronRight", 12)}<b id="breadcrumb-page">${pages[ui.page]}</b></div><div class="topbar-actions"><button class="topbar-search" data-action="focus-search">${icon("search", 15)}<span id="workspace-search-label">搜索与跳转</span><kbd>⌘K</kbd></button><button class="service-chip ${isReal() ? "is-real" : ""}" data-action="help"><i></i><span data-service-status>${isReal() ? state.publicMode === true ? "下载服务在线" : "本机服务在线" : "交互原型"}</span></button><button class="icon-button theme-toggle" data-action="toggle-theme" aria-label="${theme === "light" ? "切换到深色界面" : "切换到浅色界面"}" title="切换深色 / 浅色">${icon("moon", 17).replace("<svg", '<svg class="theme-moon"')}${icon("sun", 17).replace("<svg", '<svg class="theme-sun"')}</button><button class="icon-button notification-button" data-action="history" aria-label="查看活动记录">${icon("bell", 18)}<span class="notification-dot"></span></button></div></header><div id="service-warning" role="alert"></div><div class="page-content" id="page-content"></div></main><div id="batch-root"></div>`;
  renderPage();
}
function activeWork() {
  const jobs = (state.jobs || []).filter((job) => ["running", "queued", "paused"].includes(job.status));
  const processing = (state.processingJobs || []).filter((job) => ["running", "queued", "paused"].includes(job.status));
  const scans = (state.scans || []).filter((scan) => ["running", "queued"].includes(scan.status));
  return { jobs, processing, scans, total: jobs.length + processing.length + scans.length };
}
function trayLine(kind, title, progress, tone) {
  const pct = Math.max(0, Math.min(100, Math.round(Number(progress) || 0)));
  return `<span class="tray-line"><span class="tray-line-top"><span class="tray-kind tone-${tone}">${kind}</span><span class="tray-title">${esc(title)}</span><span class="tray-pct">${pct}%</span></span><span class="tray-track"><i class="tone-${tone}" style="width:${pct}%"></i></span></span>`;
}
function renderTray() {
  const work = activeWork();
  const running = [
    ...work.jobs.filter((job) => job.status === "running").map((job) => trayLine("下载", findAsset(job.assetId)?.title || "下载任务", job.progress, "jade")),
    ...work.processing.filter((job) => job.status === "running").map((job) => trayLine("处理", findAsset(job.assetId)?.title || "处理任务", job.progress, "blue")),
    ...work.scans.map((scan) => trayLine("扫描", scan.catalog?.title || scan.url || "扫描任务", scan.status === "running" ? Math.min(95, (Number(scan.pagesVisited) || 0) / Math.max(1, Number(scan.limit) || 20) * 100) : 0, "amber")),
  ];
  const shown = running.slice(0, 2).join("");
  const rest = work.total - Math.min(2, running.length);
  if (!work.total) return `<button class="live-tray is-idle" data-action="navigate" data-page="queue"><span class="tray-head"><span class="tray-dot"></span><span>空闲</span><span class="tray-meta">没有进行中的任务</span></span></button>`;
  return `<button class="live-tray" data-action="navigate" data-page="queue"><span class="tray-head"><span class="tray-dot is-live"></span><span>正在进行</span><span class="tray-meta">${work.total} 项</span></span>${shown}${rest > 0 ? `<span class="tray-rest">另有 ${rest} 项排队或暂停</span>` : ""}</button>`;
}
const animationPhase = () => `-${Math.round(performance.now() % 79200)}ms`;
function updateNav() {
  document.querySelectorAll(".nav-item[data-page]").forEach((el) => {
    el.classList.toggle("active", el.dataset.page === ui.page);
    if (el.dataset.page === ui.page) el.setAttribute("aria-current", "page");
    else el.removeAttribute("aria-current");
  });
  $("#breadcrumb-page").textContent = pages[ui.page];
  const work = activeWork();
  const counts = {
    discover: inboxCounts().pending || "",
    series: (state.series || []).length || "",
    library: state.assets.filter(isLocalAsset).length || "",
    queue: work.total || "",
    sites: state.sites.length || "",
    automation: state.rules?.filter(rule => rule.enabled).length || "",
  };
  document.querySelectorAll("[data-nav-count]").forEach((el) => {
    const value = counts[el.dataset.navCount];
    el.textContent = value || value === 0 ? String(value) : "";
    el.classList.toggle("is-live", el.dataset.navCount === "queue" && work.total > 0);
    el.classList.toggle("is-new", el.dataset.navCount === "discover" && Number(value) > 0);
  });
  const tray = $("#live-tray");
  if (tray && !tray.contains(document.activeElement)) {
    tray.innerHTML = renderTray();
    tray.style.setProperty("--phase", animationPhase());
  }
}
function currentStudioBatch() {
  return studioBatch(state, {...ui.processingDraft, selectedIds:ui.processIds});
}
function receiveStudioAssets(ids) {
  ui.processIds = ids;
  Object.assign(ui.processingDraft, {groupId:"", incomingIds:[...ids], previewId:ids[0]});
}
function studioTrack() {
  const assetId = $(".processing-media-frame")?.dataset.processingAsset;
  return ui.processingDraft.tracks.get(assetId);
}
let editMotionRegion;

function renderPage() {
  if (workspacePage(state, ui.page) !== ui.page) { ui.page = "home"; history.replaceState(null, "", "#home"); }
  ui.pendingRefresh = false;
  const focused = document.activeElement;
  const focusId = focused?.id;
  const cursor = focused?.selectionStart;
  const selectionEnd = focused?.selectionEnd;
  const lastStudioGroup = $("[data-processing-series]")?.value;
  const lastStudioList = $(".real-processing .process-pick-list");
  const studioScroll = lastStudioList && [lastStudioList.scrollLeft, lastStudioList.scrollTop];
  const scrollPositions = new Map();
  const catalogSelector = ui.page === "discover" ? "[data-discovery-catalog]" : "[data-series-id]";
  const catalogLists = () => document.querySelectorAll(catalogSelector + " .episode-list");
  const listKey = list => {
    const {seriesId, discoveryCatalog} = list.closest(catalogSelector).dataset;
    return seriesId || discoveryCatalog;
  };
  for (const list of catalogLists()) {
    if (list.scrollTop) scrollPositions.set(listKey(list), list.scrollTop);
  }
  const content = {
    home: renderHome,
    results: () => renderIntakeResult(ui.result.data, state, ui.result.view, ui.result),
    discover: renderCatalog,
    series: () => renderSeriesLibrary(state, {
      draft: ui.seriesDraft, selected: ui.episodeSelection, expanded: ui.seriesExpanded,
      filters: ui.seriesFilters, checkedSeries: ui.checkedSeries, episodeFilters: ui.episodeFilters,
      scanOptionsOpen: ui.seriesScanOptionsOpen, submittedScanId: ui.submittedSeriesScanId,
      submittingCheck: ui.seriesCheckSubmitting, coinDrafts: ui.coinDrafts, addOpen: ui.seriesAddOpen, flashScanId: ui.flashSeriesScanId,
      downloadSubmitting: ui.seriesDownloadSubmitting,
    }),
    library: renderCatalog,
    sites: () => renderSites(state),
    queue: () => renderQueue(state, ui.queueFilter, renderActivitySide()),
    process: () => isReal() ? renderRealProcess(state, { ...ui.processingDraft, selectedIds: ui.processIds }) : renderProcess(state, ui.processIds, ui.preset),
    automation: () => renderAutomation(state),
    settings: () => renderSettings(state, ui.operations),
  };
  const pageRoot = $("#page-content");
  if (!ui.pageEnter) pageRoot.classList.remove("is-entering");
  pageRoot.innerHTML = content[ui.page]();
  pageRoot.style.setProperty("--phase", animationPhase());
  if (ui.pageEnter) {
    ui.pageEnter = false;
    pageRoot.classList.remove("is-entering");
    void pageRoot.offsetWidth;
    pageRoot.classList.add("is-entering");
    clearTimeout(renderPage.enterTimer);
    renderPage.enterTimer = setTimeout(() => pageRoot.classList.remove("is-entering"), 520);
  }
  if (ui.page === "settings") syncThemeControls();
  if (scrollPositions.size) {
    for (const list of catalogLists()) {
      list.scrollTop = scrollPositions.get(listKey(list)) || 0;
    }
  }
  if (ui.page === "series" && ui.flashSeriesScanId) {
    const flashed = document.querySelector(".is-flash");
    if (flashed) {
      ui.flashSeriesScanId = "";
      flashed.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }
  if (isReal() && ui.page === "process") {
    const assetId = $(".processing-media-frame")?.dataset.processingAsset;
    const draft = ui.processingDraft;
    draft.groupId = $("[data-processing-series]")?.value || "";
    draft.previewId = assetId || "";
    if (studioScroll && lastStudioGroup === draft.groupId) {
      $(".real-processing .process-pick-list")?.scrollTo(...studioScroll);
    }
    if (assetId && !draft.tracks.has(assetId)) draft.tracks.set(assetId, newMotionDraft(draft.options.region));
    const track = studioTrack();
    const moving = Boolean($("[data-motion-editor]"));
    const selectedOnly = () => ui.processIds.length === 1 && ui.processIds[0] === assetId;
    const selectOnly = () => { ui.processIds = [assetId]; };
    $("[data-motion-mode]")?.addEventListener("change", event => {
      track.active = event.target.value === "moving";
      if (track.active && !ui.processIds.length) selectOnly();
      if (!track.points.length && !track.dirty) track.region = { ...draft.options.region };
      renderPage();
    });
    $("[data-motion-select-only]")?.addEventListener("click", () => { selectOnly(); renderPage(); });
    editMotionRegion = track && bindMotionEditor(pageRoot, track, {
      selectionOK: selectedOnly, busy: () => ui.processingBusy, notice: message => toast(message, true),
    });
    bindProcessingRegion(pageRoot, region => {
      if (moving) {
        track.region = region;
        editMotionRegion?.();
      } else draft.options.region = region;
    }, () => moving && track.points.length ? { width: Number(track.region.width), height: Number(track.region.height) } : null);
    if (ui.processingBusy) syncStudioBusy();
  }
  updateNav();
  renderBatch();
  if (focusId && document.getElementById(focusId)) {
    const replacement = document.getElementById(focusId);
    replacement.focus({ preventScroll: true });
    if (cursor != null && replacement.setSelectionRange)
      replacement.setSelectionRange(cursor, selectionEnd ?? cursor);
  }
}
function syncThemeControls() {
  const theme = document.documentElement.dataset.theme === "light" ? "light" : "dark";
  $('[data-action="toggle-theme"]')?.setAttribute("aria-label", theme === "light" ? "切换到深色界面" : "切换到浅色界面");
  document.querySelectorAll('[data-action="set-theme"]').forEach(button => {
    button.setAttribute("aria-pressed", String(button.dataset.value === theme));
  });
}
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "light" ? "#F8F7F2" : "#0B0B0A");
  try { localStorage.setItem("framekeep-theme", theme); } catch {}
  syncThemeControls();
}
function setNavigationOpen(open) {
  const wasOpen = document.body.classList.contains("nav-open");
  if (wasOpen === open) return;
  document.body.classList.toggle("nav-open", open);
  const trigger = $(".mobile-menu");
  trigger?.setAttribute("aria-expanded", String(open));
  $(".app-main").inert = open;
  $("#batch-root").inert = open;
  if (open) $(".drawer-close")?.focus({ preventScroll: true });
  else if (trigger?.getClientRects().length) trigger.focus({ preventScroll: true });
}
window.matchMedia("(max-width: 650px)").addEventListener("change", () => setNavigationOpen(false));

function navigate(page, scanId = "") {
  if (page === "discover") page = "home";
  page = workspacePage(state, page);
  if (!state || !Object.hasOwn(pages, page)) return;
  if (page === "results" && !scanId) return;
  if (page === "results" && ui.result.id !== scanId) {
    ui.result = { id: scanId, data: null, error: "", loading: false, view: freshResultView() };
  }
  if (ui.page === "series" && page !== "series") {
    ui.checkedSeries = [];
    for (const item of state.series || []) delete ui.episodeSelection[item.id];
  }
  if (ui.page === "discover" && page !== "discover") ui.discoverySelection = {};
  const changedPage = ui.page !== page;
  if (changedPage && page !== "series") ui.flashSeriesScanId = "";
  ui.page = page;
  ui.discoveryGroup = "all";
  ui.discoveryPage = 1;
  ui.pageEnter = changedPage;
  ui.focusId = "";
  if (page === "discover" && changedPage) ui.inbox = "pending";
  ui.selected.clear();
  ui.q = "";
  ui.kind = "all";
  ui.site = "all";
  ui.category = "all";
  ui.content = "all";
  ui.status = "all";
  ui.collection = "all";
  setNavigationOpen(false);
  history.replaceState(null, "", page === "results" ? `#results/${encodeURIComponent(scanId)}` : `#${page}`);
  renderPage();
  if (isReal() && page === "settings") void refreshOperations();
  if (isReal() && page === "results") void refreshResult(true);
  window.scrollTo({ top: 0, behavior: "instant" });
}
window.addEventListener("hashchange", () => {
  const id = resultRoute(location.hash);
  navigate(id ? "results" : location.hash.slice(1), id);
});
function catalogScope() {
  if (ui.page === "discover") return state.assets.filter(asset => !asset.dismissedFromDiscovery);
  return state.assets.filter(isLocalAsset);
}
function isLocalAsset(asset) {
  return isReal() ? hasOriginal(asset) : ["downloaded", "processed"].includes(asset?.status);
}
function inboxCounts(scope = state.assets.filter(asset => !asset.dismissedFromDiscovery)) {
  const local = scope.filter(isLocalAsset).length;
  return { pending: scope.length - local, locked: scope.filter(asset => asset.locked && !isLocalAsset(asset)).length, local, all: scope.length };
}
function inboxMatch(asset, inbox) {
  if (!inbox || inbox === "all") return true;
  if (inbox === "local") return isLocalAsset(asset);
  if (inbox === "locked") return asset.locked && !isLocalAsset(asset);
  return !isLocalAsset(asset);
}
function renderScanLive() {
  const live = (state.scans || []).filter(scan => ["queued", "running"].includes(scan.status)).slice(0, 3);
  if (!live.length) return "";
  return `<div class="scan-live" aria-live="polite">${live.map(scan => {
    const pct = scan.status === "running" ? Math.min(95, Math.max(4, (Number(scan.pagesVisited) || 0) / Math.max(1, Number(scan.limit) || 20) * 100)) : 0;
    return `<div class="scan-live-item"><span class="scan-live-spin" aria-hidden="true"></span><div class="scan-live-copy"><strong>${scan.status === "running" ? "正在扫描" : "等待扫描"}</strong><span>${esc(scan.catalog?.title || String(scan.url || "").replace(/^https?:\/\//, ""))}</span></div><small>已读取 ${Number(scan.pagesVisited) || 0} 页 · 发现 ${Number(scan.assetsFound) || 0} 项</small><span class="scan-live-track"><i style="width:${pct}%"></i></span><button class="btn btn-ghost" data-action="cancel-scan" data-id="${esc(scan.id)}">取消</button></div>`;
  }).join("")}</div>`;
}
function discoverySeriesGroups(assets) {
  const visible = new Map(assets.map(asset => [asset.id, asset.siteId]));
  const groups = new Map(), assigned = new Set();
  for (const series of state.series || []) {
    const ids = new Set();
    for (const entry of series.catalog?.entries || []) {
      const candidates = Array.isArray(entry.assetIds) ? entry.assetIds : [];
      for (const id of [entry.downloadAssetId, ...candidates]) {
        if (!id || !visible.has(id) || (series.siteId && visible.get(id) !== series.siteId)) continue;
        ids.add(id);
        assigned.add(id);
      }
    }
    if (ids.size || ui.discoveryGroup === `series:${series.id}`) {
      groups.set(series.id, {ids, title: series.customTitle || series.title || series.catalog?.title || series.catalog?.seriesKey || "未命名剧集"});
    }
  }
  return {groups, assigned};
}
function catalogAssets(scope = catalogScope(), seriesGroups) {
  return catalogContents(scope, seriesGroups).list;
}
function catalogContents(scope = catalogScope(), seriesGroups) {
  const library = ui.page === "library";
  const groups = library ? groupLocalAssets(state, scope) : null;
  let list = scope;
  if (ui.page === "discover" && ui.inbox && ui.inbox !== "all") list = list.filter(asset => inboxMatch(asset, ui.inbox));
  if (ui.page === "library" && isReal()) list = list.filter(asset => matchesFileCollection(asset, ui.collection));
  if (ui.collection === "favorites") list = list.filter((a) => a.favorite);
  if (ui.kind !== "all") list = list.filter((a) => a.kind === ui.kind);
  if (ui.site !== "all") list = list.filter((a) => a.siteId === ui.site);
  if (ui.category !== "all")
    list = list.filter((a) => a.category === ui.category);
  if (ui.page === "discover" && ui.discoveryGroup && ui.discoveryGroup !== "all") {
    const group = ui.discoveryGroup;
    if (group.startsWith("category:")) {
      list = list.filter(asset => asset.category === group.slice(9));
    } else {
      const {groups, assigned} = seriesGroups || discoverySeriesGroups(scope);
      const ids = groups.get(group.slice(7))?.ids;
      list = list.filter(asset => group === "unassigned" ? !assigned.has(asset.id) : ids?.has(asset.id));
    }
  }
  if (isReal()) list = list.filter((asset) => matchesContent(asset, ui.content));
  if (ui.status !== "all")
    list = list.filter((a) =>
      ui.status === "locked" ? a.locked : !a.locked && a.status === ui.status,
    );
  if (ui.q.trim()) {
    const q = ui.q.trim().toLowerCase();
    const matched = library ? librarySearchIds(groups, q) : null;
    list = list.filter((a) => matched?.has(a.id) ||
      `${a.title} ${a.category} ${a.tags.join(" ")} ${findSite(a.siteId)?.name || ""}`
        .toLowerCase()
        .includes(q),
    );
  }
  const sort = library ? ui.librarySort || "episode" : ui.sort;
  if (sort === "size") list = [...list].sort((a, b) => b.sizeMB - a.sizeMB);
  else if (sort === "name") list = [...list].sort((a, b) => a.title.localeCompare(b.title, "zh-CN"));
  if (!library) return {list};
  const visible = libraryVisibleGroups(groups, list, sort);
  return {list: visible.flatMap(group => group.items.map(item => item.asset)), groups: visible};
}
function discoveryPageSlice(list) {
  const size = [12, 24, 48].includes(ui.discoveryPageSize) ? ui.discoveryPageSize : 24;
  const pages = Math.max(1, Math.ceil(list.length / size));
  const page = Math.max(1, Math.min(ui.discoveryPage || 1, pages));
  const start = (page - 1) * size;
  return { page, pages, size, start, total: list.length, items: list.slice(start, start + size) };
}
function changeCatalogFilter(key, value) {
  if (ui[key] === value) return;
  ui[key] = value;
  if (ui.page === "discover") {
    ui.discoveryPage = 1;
    ui.focusId = "";
    if (key === "kind" && value === "image") ui.content = "all";
  }
  if (ui.page === "discover" || key !== "q") ui.selected.clear();
}
function toggleCatalogPageSelection() {
  const list = catalogAssets();
  const items = ui.page === "discover" ? discoveryPageSlice(list).items : list;
  const remove = items.every(asset => ui.selected.has(asset.id));
  for (const asset of items) {
    if (remove) ui.selected.delete(asset.id);
    else ui.selected.add(asset.id);
  }
}
function openDiscoveryPage(number) {
  if (ui.page !== "discover" || !Number.isInteger(number) || number === ui.discoveryPage) return;
  ui.discoveryPage = number;
  ui.focusId = "";
  renderPage();
  const start = $("#discovery-results-start");
  start.focus({preventScroll: true});
  if (start.getBoundingClientRect().top < 56) start.scrollIntoView({block: "start"});
}
function renderDiscoveryPager(result, compact) {
  const {page, pages, size} = result;
  const button = (number, name, text, disabled = false, current = false) =>
    `<button type="button" class="page-key" data-action="discovery-page" data-value="${number}" aria-label="${name}" ${disabled ? "disabled" : ""} ${current ? 'aria-current="page"' : ""}>${text}</button>`;
  const previous = button(page - 1, "上一页", "‹", page === 1);
  const next = button(page + 1, "下一页", "›", page === pages);
  if (compact) return pages > 1 ? `<nav class="page-quick" aria-label="快速翻页">${previous}<form id="discovery-page-jump" class="page-jump"><input type="number" inputmode="numeric" min="1" max="${pages}" name="page" value="${page}" aria-label="跳转到页码" title="输入页码后按回车" required><span>/ ${pages}</span></form>${next}</nav>` : "";
  const numbers = [...new Set([1, page - 1, page, page + 1, pages])].filter(n => n > 0 && n <= pages).sort((a,b) => a - b);
  const links = numbers.map((n,i) => (i && n - numbers[i-1] > 1 ? '<span class="page-gap" aria-hidden="true">…</span>' : "") + button(n, `第 ${n} 页`, n, false, n === page)).join("");
  return `<footer class="discovery-pagination"><label>每页<select class="filter-control" id="discovery-page-size" aria-label="每页素材数量" data-discovery-page-size>${[12,24,48].map(n => `<option value="${n}" ${size === n ? "selected" : ""}>${n} 项</option>`).join("")}</select></label>${pages > 1 ? `<nav aria-label="素材分页">${previous}${links}${next}</nav>` : ""}</footer>`;
}
function catalogFilterField(key, label, allLabel, options, library) {
  const disabled = !library && key === "content" && ui.kind === "image";
  const select = `<select class="filter-control" id="${key}-filter" aria-label="${label}" data-filter="${key}" ${disabled ? "disabled" : ""}>
    <option value="all">${allLabel}</option>${options.map(([value, text]) => `<option value="${esc(value)}" ${ui[key] === value ? "selected" : ""}>${esc(text)}</option>`).join("")}</select>`;
  return library ? select : `<label class="discovery-filter-field"><span>${label}</span>${select}</label>`;
}
function discoveryCategoryField(seriesGroups, categories, total) {
  const {groups, assigned} = seriesGroups;
  const selected = ui.discoveryGroup || "all";
  const option = (key, title) => `<option value="${esc(key)}" ${key === selected ? "selected" : ""}>${esc(title)}</option>`;
  const series = [...groups].sort(([,a],[,b]) => a.title.localeCompare(b.title, "zh-CN"))
    .map(([id, group]) => option(`series:${id}`, `${group.title}（${group.ids.size}）`));
  if ((assigned.size && assigned.size < total) || selected === "unassigned") series.push(option("unassigned","未归入剧集"));
  const ordinary = [...categories].sort((a,b) => a.localeCompare(b, "zh-CN")).map(name => option(`category:${name}`, name));
  return `<label class="discovery-filter-field"><span>素材分类</span><select class="filter-control" id="category-filter" aria-label="素材分类" data-filter="discoveryGroup">
    ${option("all", "全部分类")}${series.length ? `<optgroup label="按剧集">${series.join("")}</optgroup>` : ""}
    ${ordinary.length ? `<optgroup label="按素材分类">${ordinary.join("")}</optgroup>` : ""}</select></label>`;
}
function renderDiscoveryFilters(tabs, search, fields, seriesGroups, kindOptions) {
  const group = ui.discoveryGroup || "all";
  let groupLabel = "";
  if (group === "unassigned") groupLabel = "未归入剧集";
  else if (group.startsWith("category:")) groupLabel = group.slice(9);
  else if (group.startsWith("series:")) groupLabel = seriesGroups.groups.get(group.slice(7))?.title;
  const active = [
    ui.site !== "all" && findSite(ui.site)?.name, groupLabel,
    kindOptions.find(([id]) => id === ui.kind)?.[1],
    contentKinds.find(([id]) => id === ui.content)?.[1],
  ].filter(Boolean);
  const reset = active.length || ui.q || ui.kind !== "all";
  const open = Boolean(ui.discoveryFiltersOpen);
  const summary = reset ? `<div class="discovery-active-filters" aria-label="当前筛选">${active.map(label => `<span>${esc(label)}</span>`).join("")}<button type="button" class="btn btn-ghost" data-action="clear-filters">清除筛选</button></div>` : "";
  return `<section class="discovery-filter-panel" aria-label="素材筛选"><div class="discovery-filter-head">${tabs}${search}
    <button type="button" class="discovery-filter-toggle" id="discovery-filter-toggle" data-action="toggle-discovery-filters" aria-controls="discovery-detail-filters" aria-expanded="${open}">${icon("tune",15)}筛选${active.length ? `<span class="filter-count">${active.length}</span>` : ""}</button></div>
    <div class="discovery-filter-fields" id="discovery-detail-filters" data-expanded="${open}">${fields}</div>${summary}</section>`;
}
function libraryExportMenu(picked) {
  if (state.publicMode === true) return `<button class="btn btn-primary" data-action="export-originals" ${ui.archiveBusy ? "disabled" : ""}>${icon("download",15)}${picked ? "保存所选原件" : "保存原件"}（ZIP）</button>`;
  return `<details class="menu"><summary class="btn btn-secondary" aria-label="导出">${icon("download", 15)}<span class="secondary-label">导出</span>${icon("chevronDown", 13)}</summary><div class="menu-pop" role="menu"><button role="menuitem" data-action="export-library-manifest">${icon("list", 15)}<span><strong>素材清单</strong><small>JSON · 来源、标签与版本</small></span></button>${isReal() ? `<button role="menuitem" data-action="export-originals" ${ui.archiveBusy ? "disabled" : ""}>${icon("archive", 15)}<span><strong>原件 ZIP</strong><small>${picked ? `所选 ${picked} 项` : "当前筛选结果"}</small></span></button><button role="menuitem" data-action="export-processed" ${ui.archiveBusy ? "disabled" : ""}>${icon("wand", 15)}<span><strong>成品 ZIP</strong><small>只含已完成的处理版本</small></span></button>` : ""}</div></details>`;
}
function renderCatalog() {
  const library = ui.page === "library";
  const all = catalogScope();
  const seriesGroups = library ? null : discoverySeriesGroups(all);
  if (!library && ui.discoveryGroup?.startsWith("series:") && !seriesGroups.groups.has(ui.discoveryGroup.slice(7))) {
    changeCatalogFilter("discoveryGroup", "all");
  }
  const {list, groups: libraryGroups} = catalogContents(all, seriesGroups);
  if (ui.selected.size && !library) {
    const visible = new Set(list.map(asset => asset.id));
    for (const id of ui.selected) if (!visible.has(id)) ui.selected.delete(id);
  }
  const result = library ? null : discoveryPageSlice(list);
  const items = result ? result.items : list;
  if (result) ui.discoveryPage = result.page;
  const categories = new Set(all.map(asset => asset.category).filter(Boolean));
  if (ui.category !== "all") categories.add(ui.category);
  if (!library && ui.discoveryGroup?.startsWith("category:")) categories.add(ui.discoveryGroup.slice(9));
  const counts = library ? null : inboxCounts(all);
  const picked = ui.selected.size;
  const heading = library
    ? pageHeading(pages.library, `${all.length} 份${isReal() ? state.publicMode === true ? "待保存原件" : "本机原件" : "演示文件"}`,
      libraryExportMenu(picked) + (state.publicMode === true ? "" : picked
        ? `<button class="btn btn-primary" data-action="process-selection">${icon("wand", 15)}处理所选 ${picked} 项</button>`
        : `<button class="btn btn-primary" data-action="navigate" data-page="process">${icon("wand", 15)}打开工作室</button>`), "档案")
    : `<header class="page-heading discovery-heading"><div class="discovery-title"><h1>发现</h1><span>${counts.pending} 项未下载</span></div>
       <div class="page-heading-actions"><button class="btn btn-secondary" data-action="scan-history" aria-label="扫描历史">${icon("clock", 15)}<span class="secondary-label">扫描历史</span></button><button class="btn btn-primary" data-action="new-scan" data-value="column">${icon("plus", 15)}拾取链接</button></div></header>`;
  const kindOptions = [["video", "视频"], ["image", "图片"]];
  const currentInbox = ui.inbox || "all";
  const inboxTabs = library ? "" : `<div class="type-tabs inbox-tabs" role="group" aria-label="收件箱范围">${[
    ["pending", "未下载"], ["locked", "需权限"], ["local", "已下载"], ["all", "全部"],
  ].filter(([key]) => key !== "locked" || counts.locked || currentInbox === key).map(([key, label]) =>
    `<button type="button" class="type-tab ${currentInbox === key ? "active" : ""}" data-action="inbox" data-value="${key}" aria-pressed="${currentInbox === key}">${label}<span>${counts[key]}</span></button>`).join("")}</div>`;
  const search = `<div class="filter-search">${icon("search", 14)}<input id="asset-search" placeholder="${library ? "搜索剧名、文件名或标签…" : "搜索名称、标签或来源…"}" aria-label="${library ? "搜索剧名、文件名或标签" : "搜索素材名称或标签"}" value="${esc(ui.q)}"></div>`;
  const fields = [
    !library && catalogFilterField("site", "来源站点", "全部来源", state.sites.map(s => [s.id, s.name]), library),
    library ? catalogFilterField("category", "素材分类", "全部分类", [...categories].map(c => [c,c]), true)
      : discoveryCategoryField(seriesGroups, categories, all.length),
    !library && catalogFilterField("kind", "素材类型", "全部类型", kindOptions, library),
    isReal() && catalogFilterField("content", "内容类型", "全部内容", contentKinds, library),
  ].filter(Boolean).join("");
  const filters = library ? `<div class="filter-bar">${search}${fields}</div>` :
    renderDiscoveryFilters(inboxTabs, search, fields, seriesGroups, kindOptions);
  const sortKey = library ? "librarySort" : "sort";
  const sort = ui[sortKey] || (library ? "episode" : "recent");
  const arrange = `<div class="arrange"><select class="filter-control sort-control" aria-label="排列顺序" data-filter="${sortKey}">${library ? `<option value="episode" ${sort === "episode" ? "selected" : ""}>按集数</option>` : ""}<option value="recent" ${sort === "recent" ? "selected" : ""}>最近发现</option><option value="size" ${sort === "size" ? "selected" : ""}>文件大小</option><option value="name" ${sort === "name" ? "selected" : ""}>名称排序</option></select><div class="view-toggle" aria-label="显示方式"><button class="icon-button ${ui.view === "grid" ? "active" : ""}" data-action="view" data-value="grid" aria-label="网格视图">${icon("grid", 14)}</button><button class="icon-button ${ui.view === "list" ? "active" : ""}" data-action="view" data-value="list" aria-label="列表视图">${icon("list", 15)}</button></div></div>`;
  const onPage = items.filter(a => ui.selected.has(a.id)).length;
  const selectAll = `<input type="checkbox" data-action="select-all" ${items.length && onPage === items.length ? "checked" : ""} ${!items.length ? "disabled" : ""} aria-label="${library ? "全选当前筛选素材" : "全选本页素材"}">`;
  let summary;
  if (library) {
    const exportHint = state.publicMode === true ? "保存原件到本机" : isReal() ? "成品 ZIP 只含已完成版本" : "演示素材 · 已自动分类";
    summary = `<div class="selection-summary"><label>${selectAll}<span>共 ${list.length} 项${ui.selected.size ? `，已选择 ${ui.selected.size} 项` : ""}</span></label><span class="selection-hint">${exportHint}</span>${arrange}</div>`;
  } else {
    const other = ui.selected.size - onPage;
    const range = list.length ? `${result.start + 1}–${result.start + items.length} / ${list.length} 项` : "0 项结果";
    summary = `<div class="discovery-results-bar" id="discovery-results-start" tabindex="-1"><div class="discovery-summary"><label>${selectAll}全选本页</label><span class="discovery-range" aria-label="当前结果范围">${range}</span>
      ${ui.selected.size ? `<span class="discovery-selection-count">已选择 ${ui.selected.size} 项${other ? `（其他页 ${other} 项）` : ""}</span>` : ""}</div><div class="discovery-arrange">${arrange}${renderDiscoveryPager(result, true)}</div></div>`;
  }
  const grid = !items.length ? renderCatalogEmpty(library, all) : library
    ? renderLibraryGroups(libraryGroups, ui, renderAsset)
    : `<div class="${ui.view === "grid" ? "asset-grid" : "asset-list"}">${items.map(renderAsset).join("")}</div>`;
  const pager = result && list.length ? renderDiscoveryPager(result, false) : "";
  const focused = ui.focusId && items.find(a => a.id === ui.focusId);
  if (!focused) ui.focusId = "";
  const inspector = focused ? `<aside class="inspector ${ui.inspectorEnter ? "is-entering" : ""}" aria-label="${library ? "文件详情" : "素材详情"}">${renderAssetInspector(focused, list)}</aside>` : "";
  ui.inspectorEnter = false;
  const layout = `browse-layout ${library ? "is-library" : "is-discovery"}${focused ? " has-inspector" : ""}`;
  const browse = `<section class="browse-main">${filters}${summary}${grid}${pager}</section>${inspector}`;
  if (library) return `${heading}<div class="${layout}"><aside class="facet-rail" aria-label="资料库筛选">${renderFacets(all)}</aside>${browse}</div>`;
  return `${heading}${isReal() ? renderScanLive() + renderRealScans() : ""}
  ${!isReal() && ui.scan ? `<div class="scan-live"><div class="scan-live-item"><span class="scan-live-spin" aria-hidden="true"></span><div class="scan-live-copy"><strong>正在模拟扫描</strong><span>${esc(ui.scan.domain)}</span></div><small>读取目录 → 识别素材 → 分类整理</small><span class="scan-live-track"><i style="width:${ui.scan.progress}%"></i></span><button class="btn btn-ghost" data-action="cancel-scan">取消</button></div></div>` : ""}
  <div class="${layout}">${browse}</div>
  ${isReal() ? "" : `<p class="prototype-note">${icon("info", 12)}演示素材 · 扫描和下载为模拟操作。</p>`}`;
}
function renderAsset(a, episode) {
  const site = findSite(a.siteId),
    status = statusLabel(a);
  const fileView = isReal() && ui.page === "library" ? libraryFilePresentation(a, ui.collection) : { thumbnail: a.thumbnail, versionId: "original", versionCount: 0 };
  const dimensions = a.width && a.height ? `${a.width}×${a.height}` : "尺寸待定";
  const classified = isReal() && a.kind === "video" ? classificationLabel(a.classification) : "";
  const focused = ui.focusId === a.id;
  return `<article class="asset-card ${ui.selected.has(a.id) ? "selected" : ""} ${focused ? "is-focused" : ""}" data-asset="${esc(a.id)}"><div class="asset-preview" data-action="focus-asset" data-id="${esc(a.id)}" data-version="${esc(fileView.versionId)}" role="button" tabindex="0" aria-label="查看 ${esc(a.title)}"><img src="${esc(fileView.thumbnail)}" alt="${esc(a.title)}" loading="lazy"><input class="asset-check" type="checkbox" data-action="select-asset" data-id="${esc(a.id)}" aria-label="选择 ${esc(a.title)}" ${ui.selected.has(a.id) ? "checked" : ""}>${status ? `<span class="asset-top-badge ${status[1]}">${icon(status[2], 10)}${status[0]}</span>` : classified ? `<span class="asset-top-badge is-class">${esc(classified)}</span>` : ""}<span class="asset-kind">${icon(a.kind === "video" ? "video" : "image", 12)}${a.kind === "video" ? "视频" : "图片"}</span><span class="asset-duration">${a.kind === "video" ? esc(a.duration || "—") : esc(dimensions)}</span>${fileView.versionCount ? `<span class="asset-versions">${fileView.versionCount + 1} 个版本</span>` : ""}</div><div class="asset-title-line"><button class="asset-title" data-action="focus-asset" data-id="${esc(a.id)}" data-version="${esc(fileView.versionId)}">${esc(a.title)}</button><button class="asset-favorite ${a.favorite ? "is-favorite" : ""}" data-action="favorite" data-id="${esc(a.id)}" aria-label="${a.favorite ? "取消收藏" : "收藏"} ${esc(a.title)}">${icon("heart", 13)}</button></div><div class="asset-meta">${episode?.ordinal ? `<span>第 ${esc(episode.ordinal)} 集</span>` : ""}<span>${esc(shortSite(site))}</span><span>${isReal() && !a.sizeMB ? "大小待确认" : formatSize(a.sizeMB)}</span>${a.width ? `<span>${a.width >= 3840 ? "4K" : a.height >= 1080 || a.width >= 1920 ? "1080p" : a.height >= 720 ? "720p" : esc(`${a.height}p`)}</span>` : ""}</div>${a.tags.length ? `<div class="asset-tags">${a.tags.slice(0, 3).map((t) => `<span>${esc(t)}</span>`).join("")}</div>` : ""}</article>`;
}
function renderBatch() {
  const root = $("#batch-root");
  if (!["discover", "library"].includes(ui.page) || !ui.selected.size) {
    root.innerHTML = "";
    return;
  }
  const ids = [...ui.selected],
    locked = ids.some((id) => findAsset(id)?.locked);
  const entering = !root.firstElementChild ? " is-entering" : "";
  const dismiss = ui.page === "discover"
    ? `<button class="btn btn-danger" data-action="dismiss-selection">${icon("trash", 14)}删除所选</button>`
    : "";
  if (isReal()) {
    const local = ids.some(id => hasOriginal(findAsset(id)));
    if (state.publicMode === true && ui.page === "library") {
      root.innerHTML = `<div class="batch-bar${entering}"><span class="batch-count">已选 <strong>${ids.length}</strong> 项</span><button class="batch-clear" data-action="clear-selection">取消选择</button><button class="btn btn-primary" data-action="export-originals" ${ui.archiveBusy ? "disabled" : ""}>${icon("download",15)}保存所选原件（ZIP）</button></div>`;
      return;
    }
    root.innerHTML = ui.page === "library"
      ? `<div class="batch-bar${entering}"><span class="batch-count">${icon("checkCircle", 17)}已选 <strong>${ids.length}</strong> 项</span><button class="batch-clear" data-action="clear-selection">取消选择</button><span class="batch-divider"></span><button class="btn btn-secondary" data-action="export-originals" ${ui.archiveBusy ? "disabled" : ""}>${icon("archive", 14)}原件 ZIP</button><button class="btn btn-secondary" data-action="export-processed" ${ui.archiveBusy ? "disabled" : ""}>成品 ZIP</button><button class="btn btn-primary" data-action="process-selection" ${local ? "" : "disabled"}>${icon("wand", 15)}送去工作室</button></div>`
      : `<div class="batch-bar${entering}"><span class="batch-count">${icon("checkCircle", 17)}已选 <strong>${ids.length}</strong> 项</span><button class="batch-clear" data-action="clear-selection">取消选择</button><span class="batch-divider"></span>${dismiss}${local ? `<button class="btn btn-secondary" data-action="process-selection">${icon("wand", 14)}送去工作室</button>` : ""}<button class="btn btn-primary" data-action="download-selection">${icon("download", 15)}加入下载</button></div>`;
    return;
  }
  root.innerHTML = `<div class="batch-bar${entering}"><span class="batch-count">${icon("checkCircle", 17)}已选 <strong>${ui.selected.size}</strong> 项</span><button class="batch-clear" data-action="clear-selection">取消选择</button><span class="batch-divider"></span>${dismiss}${locked ? `<button class="btn btn-secondary" data-action="unlock-selection">${icon("lock", 14)}解锁内容</button>` : `<button class="btn btn-secondary" data-action="process-selection">${icon("wand", 14)}送去工作室</button>`}<button class="btn btn-primary" data-action="${ui.page === "library" ? "export-selection" : "download-selection"}">${icon("download", 15)}${ui.page === "library" ? "导出清单" : "加入下载"}</button></div>`;
}
function requestDiscoveryRemoval() {
  if (!["home", "discover"].includes(ui.page)) return;
  const ids = ui.page === "home"
    ? homePendingAssets(state, ui.homePending).items.filter(asset => ui.homePending.selected.includes(asset.id)).map(asset => asset.id)
    : catalogAssets().filter(asset => ui.selected.has(asset.id)).map(asset => asset.id);
  if (!ids.length) {
    renderPage();
    toast("当前筛选结果中没有已选素材，请重新勾选要删除的素材。");
    return;
  }
  openModal("confirm", {
    title: `移除选中的 ${ids.length} 项素材？`,
    message: "这些扫描记录将从待下载素材中移除。已下载文件、处理版本、剧集和任务记录会保留。",
    confirmAction: "confirm-dismiss", confirmLabel: "确认删除", ids,
  });
}
async function dismissDiscoverySelection() {
  const modal = ui.modal;
  if (!["home", "discover"].includes(ui.page) || modal?.confirmAction !== "confirm-dismiss" || modal.busy) return;
  modal.busy = true;
  renderModal();
  try {
    const ids = new Set(modal.ids);
    const result = isReal()
      ? await api.request("/api/assets/dismiss", { method: "POST", body: { ids: modal.ids } })
      : { dismissed: state.assets.filter(asset => ids.has(asset.id) && !asset.dismissedFromDiscovery).length };
    // A poll started before the successful write can still contain the old discovery list.
    stateReadGeneration++;
    for (const asset of state.assets) if (ids.has(asset.id)) asset.dismissedFromDiscovery = true;
    if (ui.page === "discover") for (const id of ids) ui.selected.delete(id);
    if (ui.page === "home") ui.homePending.selected = ui.homePending.selected.filter(id => !ids.has(id));
    persist();
    if (ui.modal === modal) closeModal();
    renderPage();
    toast(`已移除 ${result.dismissed} 项素材记录。`);
  } catch (error) {
    modal.busy = false;
    if (ui.modal === modal) renderModal();
    toast(error.message, true);
  }
}
function openModal(type, data = {}) {
  setNavigationOpen(false);
  lastFocus = document.activeElement;
  clearTimeout(ui.modal?.localTimer);
  ui.modal = { type, ...data };
  ui.modalOpening = true;
  renderModal();
  document.body.style.overflow = "hidden";
  if (type === "auth" && isReal() && !publicWorkspace(state)) void readLocalAuth(ui.modal);
  requestAnimationFrame(() => {
    const input = $(
      "#overlay-root input:not([type=checkbox]):not([type=hidden])",
    );
    const target = input || $("#overlay-root button");
    target?.focus();
  });
}
function closeModal() {
  if (ui.modal?.localBusy) return;
  clearTimeout(ui.modal?.localTimer);
  if (ui.modal?.busy && (ui.modal.type === "rule" || ["confirm-series-dismiss", "confirm-scan-catalog-dismiss", "confirm-series-notices", "confirm-delete-rule", "confirm-delete-processing-job"].includes(ui.modal.confirmAction))) return;
  remoteSession.close();
  ui.modal = null;
  $("#overlay-root").innerHTML = "";
  document.body.style.overflow = "";
  if (ui.pendingRefresh) renderPage();
  if (lastFocus?.isConnected) lastFocus.focus({ preventScroll: true });
  else
    document.querySelector(".nav-item.active")?.focus({ preventScroll: true });
}
const modalHeader = (title, sub = "") =>
  `<div class="modal-header"><div><h2>${title}</h2>${sub ? `<p>${sub}</p>` : ""}</div><button class="icon-button" data-action="close-modal" aria-label="关闭弹窗">${icon("x", 19)}</button></div>`;
function renderModal() {
  if (!ui.modal) return;
  const previousFocus = document.activeElement;
  const focusIdentity = {
    id: previousFocus?.id,
    action: previousFocus?.dataset?.action,
    itemId: previousFocus?.dataset?.id,
  };
  const m = ui.modal;
  let html;
  if (m.type === "auth") html = renderAuthModal(state, m.id, m);
  else if (m.type === "unlock") html = renderUnlockModal(state, m.ids);
  else if (m.type === "add-site") html = renderAddSiteModal(state);
  else if (m.type === "series-rename")
    html = `${modalHeader("修改剧名")}<form id="series-rename-form"><div class="modal-body"><div class="field"><label for="series-rename-title">自定义剧名</label><input id="series-rename-title" name="title" class="input" type="text" maxlength="100" value="${esc(m.customTitle)}" placeholder="${esc(m.sourceTitle)}"><p>留空保存会恢复来源名称「${esc(m.sourceTitle)}」。</p></div><p id="series-rename-error" class="form-error" role="alert"></p></div><div class="modal-footer"><button type="button" class="btn btn-secondary" data-action="close-modal">取消</button><button type="submit" class="btn btn-primary">保存剧名</button></div></form>`;
  else if (m.type === "rule")
    html = renderRuleModal(
      state,
      m.original || state.rules.find((r) => r.id === m.id),
    );
  else if (m.type === "scan") html = renderScanModal(m.url || "", m.scope);
  else if (m.type === "intake") html = renderIntakeModal(m);
  else if (m.type === "palette") html = renderPalette(m);
  else if (m.type === "download-options") html = renderDownloadOptions(m);
  else if (m.type === "detail")
    html = renderDetail(m.id, m.version || "original");
  else if (m.type === "scan-history") html = renderScanHistory();
  else if (m.type === "history")
    html = `${modalHeader("活动记录", "扫描、授权和素材任务的最近记录。")}<div class="modal-body"><ul class="history-list">${state.activity
      .slice(0, 15)
      .map(
        (a) =>
          `<li>${icon(a.type === "error" ? "alertCircle" : "checkCircle", 16)}<span>${esc(a.message)}</span><time>${esc(new Date(a.createdAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Bangkok" }))}</time></li>`,
      )
      .join(
        "",
      )}</ul></div><div class="modal-footer"><button class="btn btn-secondary" data-action="close-modal">关闭</button></div>`;
  else if (m.type === "confirm")
    html = `${modalHeader(esc(m.title))}<div class="modal-body"><p class="modal-message">${esc(m.message)}</p>${m.error ? `<p class="form-error" role="alert">${esc(m.error)}</p>` : ""}</div><div class="modal-footer"><button class="btn btn-secondary" data-action="close-modal" ${["confirm-series-dismiss","confirm-scan-catalog-dismiss","confirm-series-notices","confirm-delete-rule","confirm-delete-processing-job"].includes(m.confirmAction) && m.busy ? "disabled" : ""}>取消</button><button class="btn ${["confirm-dismiss", "confirm-series-dismiss", "confirm-scan-catalog-dismiss", "confirm-delete-rule", "confirm-delete-processing-job"].includes(m.confirmAction) ? "btn-danger" : "btn-primary"}" data-action="${esc(m.confirmAction)}" data-id="${esc(m.id || "")}" ${m.busy ? "disabled" : ""}>${m.busy ? "正在提交…" : esc(m.confirmLabel || "确认")}</button></div>`;
  else if (isReal()) html = renderRealHelp();
  else
    html = `${modalHeader("欢迎来到拾帧", "从发现到成品归档，一处完成。")}<div class="modal-body"><div class="tour-list">${[
      ["连接你的站点", "在来源中模拟登录，查看权限并体验逐项解锁。"],
      [
        "发现并挑选素材",
        "输入网址模拟扫描，用分类、来源和关键词筛选需要的素材。",
      ],
      ["下载与处理", "加入队列后可以暂停或重试，下载完成后应用水印模板。"],
      ["整理你的成品", "查看原件与处理版本，收藏、编辑标签，导出素材清单。"],
    ]
      .map(
        ([t, d], i) =>
          `<div class="tour-step"><span>${i + 1}</span><div><h3>${t}</h3><p>${d}</p></div></div>`,
      )
      .join(
        "",
      )}</div><div class="modal-info">${icon("info", 17)}<span>这是浏览器本地交互原型。不会登录真实账号、支付、抓取或处理媒体。可在设置中重置演示数据；导出功能生成 JSON 清单。</span></div></div><div class="modal-footer"><button class="btn btn-primary" data-action="close-modal">开始探索${icon("arrowRight", 15)}</button></div>`;
  const opening = ui.modalOpening;
  ui.modalOpening = false;
  $("#overlay-root").innerHTML =
    `<div class="modal-backdrop${opening ? " is-opening" : ""}${m.type === "palette" ? " is-top" : ""}"><section class="modal ${m.type === "detail" ? "modal-wide" : ""} ${m.type === "intake" ? "modal-intake" : ""} ${m.type === "palette" ? "modal-palette" : ""}" role="dialog" aria-modal="true" aria-label="${{ auth: "站点授权", unlock: "模拟解锁", scan: "新建扫描", intake: "拾取链接", palette: "命令面板", detail: "素材详情", "download-options": "下载选项", rule: "自动化规则", history: "活动记录" }[m.type] || "工作空间对话框"}">${html}</section></div>`;
  const modalControls = [
    ...document.querySelectorAll(
      ".modal button:not(:disabled),.modal input:not(:disabled):not([type=hidden]),.modal select:not(:disabled)",
    ),
  ];
  const matchingControl = modalControls.find(
    (el) =>
      (focusIdentity.id && el.id === focusIdentity.id) ||
      (focusIdentity.action &&
        el.dataset.action === focusIdentity.action &&
        el.dataset.id === focusIdentity.itemId),
  );
  (matchingControl || modalControls[0])?.focus({ preventScroll: true });
}
function renderScanModal(url, scope) {
  return `${modalHeader("新建扫描", "选择素材来源和范围，预览本次扫描任务。")}<form id="scan-form"><div class="modal-body"><div class="field"><label for="scan-url">站点或栏目链接</label><input class="input" id="scan-url" name="url" type="text" placeholder="https://example.com/collection" value="${esc(url)}" required><div id="scan-error" class="form-error"></div></div><div class="field-inline"><div class="field"><label for="scan-depth">扫描深度</label><select class="select" id="scan-depth" name="depth"><option value="${isReal() ? 0 : 1}" ${scope === "page" ? "selected" : ""}>仅当前页面</option>${scope === "column" && isReal() ? '<option value="1" selected>读取链接页（1 层）</option>' : ""}<option value="3" ${scope === "page" || (scope === "column" && isReal()) ? "" : "selected"}>最多 3 层页面</option><option value="5">最多 5 层页面</option></select></div><div class="field"><label for="scan-limit">最大页面数</label><input class="input" id="scan-limit" name="limit" type="number" min="1" max="${isReal() ? 100 : 1000}" value="${scope === "page" ? 1 : isReal() ? 20 : 100}" required></div></div><div class="field"><label>素材类型</label><div class="scan-options"><label><input type="checkbox" name="video" checked>视频</label><label><input type="checkbox" name="image" checked>图片</label></div></div><div class="modal-info">${icon("info", 17)}<span>${isReal() ? "扫描会真实访问所选页面并发现可访问媒体。需要登录的站点请先保存会话；付费、DRM 或站点限制可能导致无法扫描或下载。" : "将使用演示素材模拟扫描过程。新增结果会标记为演示，不会访问目标站点或下载实际文件。"}</span></div></div><div class="modal-footer"><button type="button" class="btn btn-secondary" data-action="close-modal">取消</button><button class="btn btn-primary" type="submit">${icon("search", 16)}${isReal() ? "开始扫描" : "开始模拟扫描"}</button></div></form>`;
}
function renderDetail(id, version) {
  const a = findAsset(id);
  if (!a) return modalHeader("素材不存在");
  if (isReal()) return renderRealDetail(a, { versionId: version, compare: ui.modal?.compare });
  const site = findSite(a.siteId),
    processed = version === "processed" && a.versions.length;
  const latest = a.versions.at(-1);
  const versionPreview = getVersionPreview(latest);
  return `${modalHeader(esc(a.title), `${esc(site?.name || "未知来源")} · ${a.kind === "video" ? "视频" : "图片"} · ${a.width} × ${a.height}`)}<div class="detail-preview"><img src="${esc(a.thumbnail)}" alt="${esc(a.title)}"><span class="detail-label">${processed ? "处理版本示意 · 未实际处理" : "演示封面 · 非实际媒体播放"}</span>${processed && versionPreview.showWatermark ? `<span class="process-watermark position-${versionPreview.position}" style="opacity:${versionPreview.opacity / 100}">${esc(versionPreview.text)}</span>` : ""}</div><div class="modal-body"><div class="row" style="justify-content:space-between;margin-bottom:22px"><div class="version-switch"><button class="${!processed ? "active" : ""}" data-action="detail-version" data-value="original">原始文件</button><button class="${processed ? "active" : ""}" data-action="detail-version" data-value="processed" ${!a.versions.length ? "disabled" : ""}>处理版本 ${a.versions.length}</button></div>${a.locked ? '<span class="badge badge-amber">待解锁</span>' : `<span class="badge badge-green">${a.status === "processed" ? "已处理" : a.status === "downloaded" ? "已下载" : "已发现"}</span>`}</div><dl class="detail-grid"><div><dt>来源站点</dt><dd>${esc(site?.domain || "—")}</dd></div><div><dt>文件大小</dt><dd>${formatSize(a.sizeMB)}</dd></div><div><dt>素材分类</dt><dd>${esc(a.category)}</dd></div><div><dt>发现日期</dt><dd>${esc(a.date)}</dd></div></dl><form id="tags-form" data-id="${esc(a.id)}"><label for="detail-tags" class="field-caption">素材标签（逗号分隔）</label><div class="detail-tags"><input class="input" id="detail-tags" name="tags" value="${esc(a.tags.join("，"))}" maxlength="120"><button class="btn btn-secondary" type="submit">保存标签</button><button type="button" class="btn btn-ghost" data-action="favorite" data-id="${esc(a.id)}">${icon("heart", 15)}${a.favorite ? "已收藏" : "收藏"}</button></div></form>${a.versions.length ? `<h3 style="margin:23px 0 10px;font-size:12px">版本记录</h3>${a.versions.map((v) => `<div class="detail-version">${icon("checkCircle", 14)}<span>${esc(v.label || v.name || "水印处理")}</span><span>演示版本 · 原件保留</span></div>`).join("")}` : ""}</div><div class="modal-footer"><span class="hint">${icon("shield", 12)}原始素材不会被覆盖</span><button class="btn btn-secondary" data-action="close-modal">关闭</button>${a.locked ? `<button class="btn btn-primary" data-action="unlock-one" data-id="${esc(a.id)}">${icon("lock", 15)}解锁内容 · ${a.price} 金币</button>` : a.status === "discovered" ? `<button class="btn btn-primary" data-action="download-one" data-id="${esc(a.id)}">${icon("download", 15)}加入下载队列</button>` : `<button class="btn btn-primary" data-action="process-asset" data-id="${esc(a.id)}">${icon("wand", 15)}${a.status === "processed" ? "创建新版本" : "去处理"}</button>`}</div>`;
}
async function queueAssets(ids, options) {
  if (isReal()) {
    const { path, body } = downloadRequest({ ids }, options);
    const result = await api.request(path, { method: "POST", body });
    await refreshRealState(true);
    toast(`${result.added || 0} 项已加入下载队列${result.duplicates ? `，${result.duplicates} 项已存在` : ""}${result.blocked ? `，${result.blocked} 项暂不可下载，请检查权限` : ""}`, !!result.blocked);
    return;
  }
  const result = enqueueDownloads(state, ids);
  state = result.state;
  persist();
  renderPage();
  toast(
    `${result.added ? `${result.added} 项已加入下载队列` : "未添加新任务"}${result.duplicates ? `，${result.duplicates} 项已存在` : ""}${result.blocked ? `，${result.blocked} 项需先授权或解锁` : ""}`,
    !!result.blocked,
  );
}
async function openDownloadOptions(target) {
  const ids = target.ids || [];
  const count = target.episodeKeys?.length || ids.length;
  if (!count) throw new Error("请先选择要下载的素材。");
  const asset = ids.length === 1 ? findAsset(ids[0]) : null;
  if (asset?.kind === "image" && !target.jobId) {
    await queueAssets(ids);
    closeModal();
    return;
  }
  const batch = !!target.scanId || !!target.seriesId || ids.length > 1;
  const job = target.jobId && state.jobs.find((item) => item.id === target.jobId);
  openModal("download-options", {
    target, count, batch, title: asset?.title || "", retry: !!target.jobId,
    status: batch ? "ready" : "loading", values: job?.downloadOptions || {},
  });
  if (!batch) await loadDownloadOptions(ui.modal);
}
async function loadDownloadOptions(modal = ui.modal) {
  if (modal?.type !== "download-options" || modal.busy) return;
  modal.status = "loading";
  modal.error = "";
  renderModal();
  try {
    const capabilities = await api.request(`/api/assets/${encodeURIComponent(modal.target.ids[0])}/download-options`);
    if (!capabilities || !["hls", "dash", "http"].includes(capabilities.protocol) || typeof capabilities.selectable !== "boolean" || !Array.isArray(capabilities.heights) || !Array.isArray(capabilities.audioTracks)) {
      throw new Error("源站选项信息不完整，请重新读取或按原始默认下载。");
    }
    modal.capabilities = capabilities;
    modal.status = "ready";
  } catch (error) {
    modal.status = "error";
    modal.error = error.message;
  }
  if (ui.modal === modal) renderModal();
}
async function confirmDownloadOptions(options) {
  const modal = ui.modal;
  if (modal?.type !== "download-options" || modal.busy) return;
  modal.busy = true;
  try {
    const target = modal.target;
    if (target.jobId) {
      const { path, body } = downloadRequest(target, options);
      await api.request(path, { method: "POST", body });
      await refreshRealState(true);
      toast("已更新下载选项，任务已重新加入队列。");
    } else if (target.scanId || target.seriesId) {
      const { path, body } = downloadRequest(target, options);
      const result = await api.request(path, { method: "POST", body });
      await refreshRealState(true);
      toast(catalogDownloadMessage(result));
    } else {
      await queueAssets(target.ids, options);
    }
    if (ui.modal === modal) closeModal();
  } finally {
    modal.busy = false;
  }
}
function processAssets(ids) {
  if (publicWorkspace(state)) { navigate("library"); return; }
  const valid = ids.filter((id) =>
    isReal() ? hasOriginal(findAsset(id)) : ["downloaded", "processed"].includes(findAsset(id)?.status),
  );
  if (!valid.length) {
    toast("请先下载素材，再进行水印处理。", true);
    return;
  }
  if (isReal()) receiveStudioAssets(valid);
  else ui.processIds = valid;
  closeModal();
  navigate("process");
}
function exportManifest(ids) {
  const assets = isReal() ? state.assets.filter((asset) => ids ? ids.includes(asset.id) : ui.page !== "library" || asset.status === "downloaded") : getExportAssets(state, ui.page, ids);
  const data = {
    application: "拾帧 Framekeep",
    prototype: !isReal(),
    exportedAt: new Date().toISOString(),
    notice: isReal() ? "本地素材记录清单；媒体文件请在详情中保存原件。" : "仅为演示素材清单，不包含实际下载或处理后的媒体文件。",
    assets,
    sites: state.sites.map((s) => ({
      id: s.id,
      name: s.name,
      domain: s.domain,
    })),
  };
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = `framekeep-manifest-${new Date().toISOString().slice(0, 10)}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast(`已导出 ${assets.length} 项素材的 JSON ${isReal() ? "" : "演示"}清单。`);
}
function exportLibraryArchive(variant) {
  const assets = ui.selected.size
    ? state.assets.filter(asset => ui.selected.has(asset.id) && hasOriginal(asset))
    : catalogAssets().filter(hasOriginal);
  if (!assets.length) throw new Error("当前没有可保存的文件，请调整筛选或选择素材。");
  if (publicWorkspace(state)) variant = "original";
  submitArchiveDownload("/api/library/archive-file", {ids:assets.map(asset => asset.id), variant}, "保存素材原件");
  toast(`已请求保存 ${assets.length} 项${variant === "latest" ? "成品" : "原件"}，请查看浏览器下载记录。`);
}
function startScan(values) {
  const url = normalizeSiteUrl(values.get("url"));
  if (!url) {
    $("#scan-error").textContent = "请输入有效的 HTTP 或 HTTPS 网址。";
    return;
  }
  const kinds = ["video", "image"].filter((k) => values.has(k));
  if (!kinds.length) {
    $("#scan-error").textContent = "至少选择一种素材类型。";
    return;
  }
  if (ui.scan) {
    $("#scan-error").textContent = "已有扫描正在进行，请等待完成或先取消。";
    return;
  }
  const domain = new URL(url).hostname;
  let site = state.sites.find((s) => s.domain === domain);
  if (site && !["connected", "public"].includes(site.auth)) {
    openModal("auth", { id: site.id });
    toast("请先连接站点的演示会话，再发起扫描。", true);
    return;
  }
  if (!site) {
    site = {
      id: uniqueId("site"),
      name: domain,
      domain,
      initials: domain.slice(0, 2).toUpperCase(),
      auth: "public",
      access: "公开访问",
      updated: "尚未扫描",
      count: 0,
    };
    state.sites.push(site);
  }
  ui.scan = {
    domain,
    siteId: site.id,
    progress: 0,
    kinds,
    limit: Math.min(1000, Math.max(1, Number(values.get("limit")))),
    depth: Number(values.get("depth")),
  };
  closeModal();
  navigate("discover");
  persist();
  scanTimer = setInterval(() => {
    if (!ui.scan) return;
    ui.scan.progress += 20;
    if (ui.scan.progress < 100) {
      if (ui.page === "discover") renderPage();
      return;
    }
    clearInterval(scanTimer);
    const task = ui.scan;
    const samples = createInitialState()
      .assets.filter((a) => task.kinds.includes(a.kind))
      .slice(0, Math.min(4, task.limit));
    const run = state.activity.filter((a) => a.type === "scan").length + 1;
    const found = samples.map((a, i) => ({
      ...a,
      id: uniqueId("scan"),
      title: `${a.title} · 演示 ${run}`,
      siteId: task.siteId,
      status: "discovered",
      locked: false,
      price: 0,
      favorite: false,
      versions: [],
      tags: [a.category, "演示新增"],
    }));
    state.assets.unshift(...found);
    const s = findSite(task.siteId);
    s.updated = "刚刚扫描";
    s.count = state.assets.filter((a) => a.siteId === s.id).length;
    activity(
      `演示扫描 ${task.domain} 完成：新增 ${found.length} 项，范围 ${task.depth} 层 / ${task.limit} 页`,
      "scan",
    );
    ui.scan = null;
    persist();
    renderPage();
    toast(`扫描演示完成，新增 ${found.length} 项素材。`);
  }, 650);
}
let backdropPress = null;
document.addEventListener("error", event => {
  if (event.target instanceof HTMLImageElement && event.target.hasAttribute("data-series-poster")) event.target.remove();
}, true);

document.addEventListener("pointerdown", (event) => { backdropPress = event.target; }, true);
document.addEventListener("click", (event) => {
  if (event.target.classList?.contains("modal-backdrop") && backdropPress === event.target && ui.modal && !ui.modal.busy) {
    const light = ["palette", "history", "help", "scan-history", "detail"].includes(ui.modal.type) || (ui.modal.type === "intake" && !String(ui.modal.url || "").trim());
    if (light) { closeModal(); return; }
  }
  const addSummary = event.target.closest?.(".series-add > summary");
  if (addSummary) {
    const details = addSummary.parentElement;
    if (details.classList.contains("is-first")) event.preventDefault();
    else {
      ui.seriesAddOpen = !details.open;
      ui.focusSeriesAdd = !details.open;
    }
  }
  document.querySelectorAll("details.menu[open]").forEach((menu) => {
    if (!menu.contains(event.target) || event.target.closest(".menu-pop [data-action]")) menu.removeAttribute("open");
  });
}, true);
document.addEventListener("click", async (event) => {
  const el = event.target.closest("[data-action]");
  if (!el) return;
  const { action, id, value, page } = el.dataset;
  if (action === "retry-service") { await boot(); return; }
  if (action === "retry-state") { await refreshRealState(true).catch((error) => toast(error.message, true)); return; }
  if (!state || el.disabled) return;
  if (isReal() && await handleRealAction(action, el)) return;
  if (action === "select-asset") {
    event.stopPropagation();
    ui.selected.has(id) ? ui.selected.delete(id) : ui.selected.add(id);
    renderPage();
    return;
  }
  const handlers = {
    "scan-history": () => openModal(isReal() ? "scan-history" : "history"),
    "home-pending-open": () => {
      ui.homePending.open = true;
      navigate("home");
      $("[data-home-pending]")?.scrollIntoView({block:"start"});
    },
    "home-pending-page": () => {
      Object.assign(ui.homePending, {page:Number(value), selected:[]});
      renderPage();
    },
    "home-pending-select": () => {
      const ids = homePendingAssets(state, ui.homePending).items.map(asset => asset.id);
      ui.homePending.selected = ids.every(id => ui.homePending.selected.includes(id)) ? [] : ids;
      renderPage();
    },
    "home-pending-download": () => {
      const reserved = reservedAssets(state);
      const ids = homePendingAssets(state, ui.homePending).items.filter(asset => ui.homePending.selected.includes(asset.id) && availableForDownload(asset,reserved)).map(asset => asset.id);
      return isReal() ? openDownloadOptions({ids}) : queueAssets(ids);
    },
    "home-pending-remove": requestDiscoveryRemoval,
    "recover-job": async () => {
      const job = state.jobs.find(item => item.id === id);
      const asset = job && findAsset(job.assetId);
      if (!asset?.pageUrl) throw new Error("缺少原页面，请重新扫描站点。");
      await api.request("/api/scans", { method: "POST", body: {siteId: asset.siteId, url: asset.pageUrl, depth: 0, limit: 1, kinds: [asset.kind]} });
      await refreshRealState(true);
      navigate("discover");
      toast("已提交原页面扫描；完成后可返回队列重试。");
    },
    navigate: () => {
      closeModal();
      if (page === "queue" && el.dataset.filter === "all") ui.queueFilter = "all";
      navigate(page);
    },
    "focus-asset": () => {
      if (window.matchMedia("(max-width: 1180px)").matches) {
        openModal("detail", { id, version: el.dataset.version || "original", compare: false });
        return;
      }
      ui.inspectorEnter = !ui.focusId;
      ui.focusId = id;
      renderPage();
      $('[data-action="close-inspector"]')?.focus({ preventScroll: true });
    },
    "close-inspector": closeAssetInspector,
    "inspector-step": () => stepInspector(Number(value)),
    inbox: () => {
      changeCatalogFilter("inbox", value);
      renderPage();
      $(`[data-action="inbox"][data-value="${CSS.escape(value)}"]`)?.focus({ preventScroll: true });
    },
    "intake-scope": () => {
      if (ui.modal?.type !== "intake") return;
      ui.modal.scope = value;
      ui.modal.explicitScope = true;
      ui.intakeScope = value;
      renderModal();
      document.querySelector(`.scope-card[data-value="${value}"]`)?.focus({ preventScroll: true });
    },
    "home-more": () => openIntake({ scope: ui.homeScope, url: ui.homeDraft }),
    "intake-recent": () => {
      if (ui.modal?.type !== "intake") return;
      ui.modal.url = value;
      if (el.dataset.scope) ui.modal.scope = el.dataset.scope;
      renderModal();
      const input = $("#intake-url");
      input?.focus();
      input?.setSelectionRange(input.value.length, input.value.length);
    },
    "facet-site": () => {
      ui.site = value;
      ui.selected.clear();
      renderPage();
    },
    "toggle-theme": () => setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light"),
    "set-theme": () => setTheme(value === "light" ? "light" : "dark"),
    "home-scope": () => {
      ui.homeScope = value;
      ui.explicitHomeScope = true;
      renderPage();
    },
    "open-nav": () => setNavigationOpen(true),
    "close-nav": () => setNavigationOpen(false),
    help: () => openModal("help"),
    history: () => openModal("history"),
    "close-modal": closeModal,
    "new-scan": () => focusHomeIntake({ scope: value }),
    "scan-site": () =>
      focusHomeIntake({ url: findSite(id)?.url || `https://${findSite(id)?.domain || ""}`, scope: "site" }),
    "cancel-scan": () => {
      clearInterval(scanTimer);
      ui.scan = null;
      renderPage();
      toast("扫描已取消。");
    },
    kind: () => {
      changeCatalogFilter("kind", value);
      renderPage();
    },
    view: () => {
      ui.view = value;
      renderPage();
    },
    collection: () => {
      ui.collection = value;
      ui.selected.clear();
      renderPage();
    },
    "clear-filters": () => {
      ui.kind = "all";
      ui.site = "all";
      ui.category = "all";
      ui.content = "all";
      ui.status = "all";
      ui.q = "";
      ui.collection = "all";
      if (ui.page === "discover") {
        ui.discoveryPage = 1;
        ui.discoveryGroup = "all";
        ui.focusId = "";
        ui.selected.clear();
      }
      renderPage();
    },
    "select-all": () => {
      toggleCatalogPageSelection();
      renderPage();
    },
    "library-toggle-group": () => {
      const open = el.getAttribute("aria-expanded") !== "true";
      ui.librarySections.set(id, {...ui.librarySections.get(id), open});
      if (!open && el.closest(".library-group")?.querySelector(".asset-card.is-focused")) ui.focusId = "";
      renderPage();
    },
    "library-select-group": () => {
      const group = catalogContents().groups?.find(item => item.id === id);
      if (!group) return;
      const remove = group.items.every(({asset}) => ui.selected.has(asset.id));
      for (const {asset} of group.items) {
        if (remove) ui.selected.delete(asset.id);
        else ui.selected.add(asset.id);
      }
      renderPage();
    },
    "library-more": () => {
      const group = catalogContents().groups?.find(item => item.id === id);
      if (!group) return;
      const shown = ui.librarySections.get(id)?.shown || LIBRARY_PAGE_SIZE;
      ui.librarySections.set(id, {open: true, shown: shown + LIBRARY_PAGE_SIZE});
      renderPage();
      if (group.items[shown]) $(`[data-asset="${CSS.escape(group.items[shown].asset.id)}"] [data-action="focus-asset"]`)?.focus({preventScroll:true});
    },
    "toggle-discovery-filters": () => {
      ui.discoveryFiltersOpen = !ui.discoveryFiltersOpen;
      renderPage();
    },
    "discovery-page": () => openDiscoveryPage(Number(value)),
    "dismiss-selection": requestDiscoveryRemoval,
    "confirm-dismiss": dismissDiscoverySelection,
    "clear-selection": () => {
      ui.selected.clear();
      renderPage();
    },
    favorite: () => {
      const a = findAsset(id);
      if (a) {
        a.favorite = !a.favorite;
        persist();
        renderPage();
        if (ui.modal?.type === "detail") renderModal();
      }
    },
    detail: () => openModal("detail", { id, version: el.dataset.version || "original", compare: el.dataset.compare === "true" }),
    "detail-version": () => {
      ui.modal.version = value;
      renderModal();
    },
    "download-selection": () => queueAssets([...ui.selected]),
    "download-one": () => {
      closeModal();
      queueAssets([id]);
    },
    "process-selection": () => processAssets([...ui.selected]),
    "process-asset": () => processAssets([id]),
    "unlock-selection": () => openModal("unlock", { ids: [...ui.selected] }),
    "unlock-one": () => openModal("unlock", { ids: [id] }),
    "unlock-site": () =>
      openModal("unlock", {
        ids: state.assets
          .filter((a) => a.siteId === id && a.locked)
          .map((a) => a.id),
      }),
    "confirm-unlock": () => {
      const ids = (el.dataset.ids || "").split(",");
      const before = state.assets.filter(
        (a) => ids.includes(a.id) && a.locked,
      ).length;
      state = unlockAssets(state, ids);
      const after = state.assets.filter(
        (a) => ids.includes(a.id) && a.locked,
      ).length;
      persist();
      closeModal();
      renderPage();
      toast(
        `已模拟解锁 ${before - after} 项；未产生真实付款。${after ? "其余条目请先连接站点。" : ""}`,
        !!after,
      );
    },
    "auth-site": () => openModal("auth", { id }),
    "add-site": () => openModal("add-site"),
    "connect-site": () => {
      state = setSiteAuth(state, id, "connected");
      persist();
      renderPage();
      renderModal();
      toast("演示会话已连接，可继续扫描或解锁。");
    },
    "disconnect-site": () => {
      state = setSiteAuth(state, id, "disconnected");
      persist();
      renderPage();
      renderModal();
      toast("已断开演示会话。");
    },
    "queue-filter": () => {
      ui.queueFilter = value;
      renderPage();
    },
    "job-action": () => {
      state = transitionJob(state, id, value);
      persist();
      renderPage();
    },
    "pause-all": () => {
      for (const job of [...state.jobs])
        if (["running", "queued"].includes(job.status))
          state = transitionJob(state, job.id, "pause");
      persist();
      renderPage();
      toast("所有运行和等待中的任务已暂停。");
    },
    "toggle-process": () => {
      ui.processIds = ui.processIds.includes(id)
        ? ui.processIds.filter((i) => i !== id)
        : [...ui.processIds, id];
      renderPage();
    },
    "start-process": () => {
      if (!ui.processIds.length) {
        toast("先选择需要处理的已下载素材。", true);
        return;
      }
      if (["add", "both"].includes(ui.preset.mode) && !ui.preset.text.trim()) {
        toast("请输入水印文字。", true);
        return;
      }
      ui.preset.name = {
        add: "添加品牌水印",
        remove: "固定区域去水印",
        both: "去水印 + 品牌水印",
      }[ui.preset.mode];
      const r = startProcessing(state, ui.processIds, ui.preset);
      state = r.state;
      persist();
      ui.queueFilter = "all";
      navigate("queue");
      toast(
        `已创建 ${r.added} 个模拟处理任务${r.duplicates ? `，${r.duplicates} 个任务正在进行` : ""}；完成后可在成品库查看。`,
      );
    },
    "new-rule": () => openModal("rule"),
    "edit-rule": () => openModal("rule", { id }),
    "toggle-rule": () => {
      const rule = state.rules.find((r) => r.id === id);
      if (rule) {
        rule.enabled = !rule.enabled;
        persist();
        renderPage();
        toast(
          `${rule.enabled ? "已启用" : "已暂停"}规则演示配置；原型不会定时执行。`,
        );
      }
    },
    "delete-rule": () =>
      openModal("confirm", {
        title: "删除这条规则？",
        message: "只会删除当前浏览器中的演示规则，不会影响已有素材。",
        confirmAction: "confirm-delete-rule",
        id,
      }),
    "confirm-delete-rule": () => {
      state.rules = state.rules.filter((r) => r.id !== id);
      persist();
      closeModal();
      renderPage();
      toast("规则已删除。");
    },
    "export-manifest": () => exportManifest(),
    "export-library-manifest": () => exportManifest(ui.selected.size ? [...ui.selected] : catalogAssets().map(asset => asset.id)),
    "export-selection": () => exportManifest([...ui.selected]),
    "reset-demo": () =>
      openModal("confirm", {
        title: "重置演示工作空间？",
        message:
          "当前浏览器中新建的演示站点、任务、标签和规则将恢复为初始示例。实际文件不会受到影响。",
        confirmAction: "confirm-reset",
      }),
    "confirm-reset": () => {
      clearInterval(scanTimer);
      ui.scan = null;
      state = createInitialState();
      ui.processIds = [];
      ui.selected.clear();
      persist();
      closeModal();
      navigate("discover");
      toast("演示数据已恢复。");
    },
    "focus-search": () => openPalette(),
    "palette-run": () => runPaletteItem(Number(value)),
  };
  handlers[action]?.();
});
document.addEventListener("input", (event) => {
  const el = event.target;
  if (el.id === "home-pending-query") {
    Object.assign(ui.homePending, {query:el.value, page:1, selected:[]});
    if (!event.isComposing) renderPage();
    return;
  }
  if (el.id === "palette-q" && ui.modal?.type === "palette") {
    ui.modal.q = el.value;
    ui.modal.index = 0;
    updatePaletteResults(ui.modal);
    return;
  }
  if (el.form?.id === "intake-form" && ui.modal?.type === "intake") {
    if (el.name === "url") {
      ui.modal.url = el.value;
      const inferred = isReal() && !ui.modal.explicitScope && suggestScope(normalizeScanUrl(el.value));
      if (inferred && inferred !== ui.modal.scope) {
        const end = el.selectionStart;
        ui.modal.scope = inferred;
        renderModal();
        const input = $("#intake-url");
        input.focus({ preventScroll: true });
        input.setSelectionRange(end, end);
      }
    }
    else {
      const draft = ui.modal.opts[ui.modal.scope] ||= {};
      draft[el.name] = el.type === "checkbox" ? el.checked : el.value;
    }
    el.form.querySelector(".form-error")?.replaceChildren();
    return;
  }
  if (isReal() && (el.dataset.processingField || el.dataset.processingRegion)) {
    updateProcessingDraft(el);
    return;
  }
  if (isReal() && el.dataset.coinBudget) {
    updateCoinDraft(el.dataset.coinBudget, "budget", el.value);
    if (!event.isComposing) renderPage();
    return;
  }
  if (el.dataset.catalogFilter === "query") {
    if (!event.isComposing) applyDiscoveryFilter(el);
    return;
  }
  if (el.dataset.seriesFilter === "query" || el.dataset.episodeFilter === "query") {
    if (!event.isComposing) applySeriesFilter(el);
    return;
  }
  if (el.form?.id === "series-scan-form") ui.seriesDraft[el.name] = el.value;
  if (el.id === "intake-group-query") {
    ui.result.view.query = el.value;
    ui.result.view.groupPage = 1;
    if (!event.isComposing) renderPage();
    return;
  }
  if (el.id === "home-url") {
    ui.homeDraft = el.value;
    const inferred = isReal() && !ui.explicitHomeScope && suggestScope(normalizeScanUrl(el.value));
    if (inferred && inferred !== ui.homeScope) {
      ui.homeScope = inferred;
      for (const button of document.querySelectorAll('[data-action="home-scope"]')) {
        button.classList.toggle("active", button.dataset.value === inferred);
        button.setAttribute("aria-pressed", String(button.dataset.value === inferred));
      }
      $(".home-scope-hint").textContent = intakeScopes.find(([id]) => id === inferred)[2];
      const submit = $('#home-intake-form button[type="submit"]');
      if (submit) submit.innerHTML = `${inferred === "single" ? "直接下载" : "开始拾取"}<kbd>↵</kbd>`;
    }
  }
  if (el.id === "quick-scan-url") ui.quickDraft = el.value;
  if (el.id === "asset-search") {
    if (!event.isComposing && ui.q !== el.value) {
      changeCatalogFilter("q", el.value);
      renderPage();
    }
  }
  if (el.dataset.preset) {
    const key = el.dataset.preset;
    ui.preset[key] = key === "opacity" ? Number(el.value) : el.value;
    if (el.tagName !== "SELECT") refreshPresetPreview();
  }
});
document.addEventListener("compositionend", event => {
  const el = event.target;
  if (el.id === "intake-group-query") { ui.result.view.query = el.value; ui.result.view.groupPage = 1; renderPage(); return; }
  if (el.id === "asset-search" && ui.q !== el.value) {
    changeCatalogFilter("q", el.value);
    renderPage();
  }
  if (el.dataset.catalogFilter === "query") applyDiscoveryFilter(el);
  if (el.dataset.seriesFilter === "query" || el.dataset.episodeFilter === "query") applySeriesFilter(el);
});
function applyDiscoveryFilter(el) {
  if (ui.page !== "discover") return;
  const { id, catalogFilter } = el.dataset;
  const filter = ui.discoveryFilters[id] ||= {};
  if (filter[catalogFilter] === el.value) return;
  filter[catalogFilter] = el.value;
  delete ui.discoverySelection[id];
  ui.discoveryExpanded[id] = true;
  const list = el.closest("[data-discovery-catalog]")?.querySelector(".episode-list");
  if (list) list.scrollTop = 0;
  renderPage();
}
function applySeriesFilter(el) {
  if (ui.page !== "series") return;
  const { seriesFilter, episodeFilter, id } = el.dataset;
  if (seriesFilter) {
    if (ui.seriesFilters[seriesFilter] === el.value) return;
    ui.seriesFilters[seriesFilter] = el.value;
    ui.seriesFilters.page = 1;
    ui.checkedSeries = [];
  } else {
    const filters = ui.episodeFilters[id] ||= {};
    if (filters[episodeFilter] === el.value) return;
    filters[episodeFilter] = el.value;
    delete ui.episodeSelection[id];
    ui.seriesExpanded[id] = true;
    const list = el.closest("[data-series-id]")?.querySelector(".episode-list");
    if (list) list.scrollTop = 0;
  }
  renderPage();
}
function focusWorkspaceSearch() {
  if (ui.page === "series") {
    $('[data-series-filter="query"]')?.focus();
    return;
  }
  if (ui.page !== "library") { openModal("palette", {q:"",index:0}); return; }
  $("#asset-search")?.focus();
}
function syncStudioBusy() {
  const selector = '[data-action="start-process"], [data-action="process-chunlou-glyph"]';
  for (const button of document.querySelectorAll(selector)) {
    if (ui.processingBusy && !button.disabled) {
      button.dataset.studioSubmitting = "true";
      button.disabled = true;
    } else if (!ui.processingBusy && button.hasAttribute("data-studio-submitting")) {
      delete button.dataset.studioSubmitting;
      button.disabled = false;
    }
  }
}
async function submitStudioRequest({path, body}) {
  if (ui.processingBusy) return;
  ui.processingBusy = true;
  syncStudioBusy();
  try {
    const result = await api.request(path, {method:"POST", body});
    await refreshRealState(true);
    const name = body.options.method === "chunlou-glyph" ? "春楼飘字精修" : "处理";
    toast(`已创建 ${Number(result.added) || 0} 个${name}任务${result.duplicates ? `，${result.duplicates} 项相同参数已完成或正在处理` : ""}${result.blocked ? `，${result.blocked} 项暂不可处理` : ""}。`);
  } finally {
    ui.processingBusy = false;
    syncStudioBusy();
    if (ui.page === "process" && !workspaceInteractionActive()) renderPage();
  }
}
function updateProcessingDraft(element) {
  const { processingField: field, processingRegion: region } = element.dataset;
  const options = ui.processingDraft.options;
  if (field) options[field] = element.value;
  const track = studioTrack();
  const moving = Boolean($("[data-motion-editor]"));
  const box = moving ? track.region : options.region;
  if (region) box[region] = element.value;
  updateProcessingPreview($("#page-content"), { ...options, region: box });
  if (moving && region) editMotionRegion?.();
  if (field && element.type === "range") {
    document.querySelectorAll(`[data-processing-output="${field}"]`).forEach((output) => { output.textContent = `${element.value}%`; });
  }
  if (field === "position") {
    element.closest(".corner-picker")?.querySelectorAll(".corner").forEach((corner) => corner.classList.toggle("active", corner.contains(element)));
  }
}
function refreshPresetPreview() {
  const focused = document.activeElement;
  const name = focused?.dataset?.preset;
  const cursor = focused?.selectionStart;
  const scroll = window.scrollY;
  renderPage();
  if (name) {
    const input = document.querySelector(`[data-preset="${name}"]`);
    input?.focus({ preventScroll: true });
    if (cursor != null && input?.setSelectionRange)
      input.setSelectionRange(cursor, cursor);
  }
  window.scrollTo({ top: scroll, behavior: "instant" });
}
document.addEventListener("change", (event) => {
  const el = event.target;
  if (el.hasAttribute("data-home-asset-check")) {
    const checked = new Set(ui.homePending.selected);
    if (el.checked) checked.add(el.dataset.homeAssetCheck);
    else checked.delete(el.dataset.homeAssetCheck);
    ui.homePending.selected = [...checked];
    renderPage();
    return;
  }
  if (isReal() && el.dataset.coinUnlock) {
    updateCoinDraft(el.dataset.coinUnlock, "enabled", el.checked);
    renderPage();
    return;
  }
  if (isReal() && el.hasAttribute("data-real-version") && ui.modal?.type === "detail") {
    ui.modal.version = el.value;
    if (el.value === "original") ui.modal.compare = false;
    renderModal();
    return;
  }
  if (isReal() && el.hasAttribute("data-processing-series")) {
    ui.processingDraft.groupId = el.value;
    ui.processingDraft.previewId = "";
    ui.processIds = [];
    renderPage();
    return;
  }
  if (isReal() && el.hasAttribute("data-processing-preview")) {
    ui.processingDraft.previewId = el.value;
    renderPage();
    return;
  }
  if (isReal() && (el.dataset.processingField || el.dataset.processingRegion)) {
    updateProcessingDraft(el);
    if (el.dataset.processingField === "mode") renderPage();
    return;
  }
  if (el.form?.id === "series-scan-form") ui.seriesDraft[el.name] = el.value;
  if (el.dataset.catalogFilter) {
    applyDiscoveryFilter(el);
    return;
  }
  if (el.dataset.seriesFilter || el.dataset.episodeFilter) {
    applySeriesFilter(el);
    return;
  }
  if (el.id === "intake-category") {
    Object.assign(ui.result.view, {category: el.value, groupPage: 1, groupKey: null, episodeKey: null, episodePage: 1, mediaPage: 1, episodeMediaPage: 1});
    renderPage();
    return;
  }
  if (el.hasAttribute("data-result-check")) {
    if (el.checked) ui.result.view.selected.add(el.dataset.resultCheck);
    else ui.result.view.selected.delete(el.dataset.resultCheck);
    renderPage();
    return;
  }
  if (el.hasAttribute("data-series-check")) {
    const checked = new Set(ui.checkedSeries);
    if (el.checked) checked.add(el.dataset.seriesCheck); else checked.delete(el.dataset.seriesCheck);
    ui.checkedSeries = [...checked];
    renderPage();
    return;
  }
  if (el.dataset.episodeScan) {
    const scanId = el.dataset.episodeScan;
    if (ui.page === "discover") ui.discoveryExpanded[scanId] = true;
    const selection = ui.page === "discover" ? ui.discoverySelection : ui.episodeSelection;
    const selected = new Set(selection[scanId] || []);
    if (el.checked) selected.add(el.value); else selected.delete(el.value);
    selection[scanId] = [...selected];
    renderPage();
    return;
  }
  if (el.hasAttribute("data-discovery-page-size") && ui.page === "discover") {
    ui.discoveryPageSize = Number(el.value);
    ui.discoveryPage = 1;
    ui.focusId = "";
    renderPage();
    $("#discovery-results-start").focus();
    return;
  }
  if (el.dataset.filter) {
    changeCatalogFilter(el.dataset.filter, el.value);
    renderPage();
  }
  if (el.dataset.preset && el.tagName === "SELECT") {
    ui.preset[el.dataset.preset] = el.value;
    renderPage();
  }
});
document.addEventListener("toggle", (event) => {
  if (!event.target.isConnected) return;
  if (event.target.hasAttribute("data-home-pending")) ui.homePending.open = event.target.open;
  const id = event.target.dataset?.seriesCatalog;
  if (id) ui.seriesExpanded[id] = event.target.open;
  const scanId = event.target.dataset?.discoveryCatalog;
  if (scanId) ui.discoveryExpanded[scanId] = event.target.open;
  if (event.target.hasAttribute("data-series-scan-settings")) ui.seriesScanOptionsOpen = event.target.open;
  if (event.target.hasAttribute("data-series-add") && !event.target.classList.contains("is-first")) {
    ui.seriesAddOpen = event.target.open;
    if (event.target.open && ui.focusSeriesAdd) event.target.querySelector("#series-url")?.focus({ preventScroll: true });
    ui.focusSeriesAdd = false;
  }
}, true);
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (form.id === "discovery-page-jump") {
    event.preventDefault();
    openDiscoveryPage(Number(new FormData(form).get("page")));
    return;
  }
  if (
    ![
      "quick-scan-form",
      "home-intake-form",
      "intake-form",
      "series-scan-form",
      "series-rename-form",
      "scan-form",
      "add-site-form",
      "tags-form",
      "classification-form",
      "download-options-form",
      "rule-form",
      "settings-form",
    ].includes(form.id)
  )
    return;
  event.preventDefault();
  const values = new FormData(form);
  if (form.id === "quick-scan-form") {
    ui.quickDraft = "";
    openIntake({ url: values.get("url"), scope: "page" });
    return;
  }
  if (form.id === "home-intake-form") {
    await submitHomeIntake(form, values);
    return;
  }
  if (form.id === "intake-form") {
    await submitIntake(form, values, ui.modal?.scope || "series", ui.modal?.siteId || "");
    return;
  }
  if (isReal()) {
    await submitRealForm(form, values);
    return;
  }
  if (form.id === "scan-form") {
    startScan(values);
    return;
  }
  if (form.id === "add-site-form") {
    const url = normalizeSiteUrl(values.get("url"));
    if (!url) {
      toast("请输入有效的 HTTP 或 HTTPS 网址。", true);
      return;
    }
    const domain = new URL(url).hostname;
    if (state.sites.some((s) => s.domain === domain)) {
      toast("这个站点已存在，可直接管理授权或扫描。", true);
      return;
    }
    const name = String(values.get("name") || domain)
      .trim()
      .slice(0, 60);
    state.sites.push({
      id: uniqueId("site"),
      name: name || domain,
      domain,
      initials: (name || domain).slice(0, 2).toUpperCase(),
      auth: "disconnected",
      access: "未连接",
      updated: "刚刚添加",
      count: 0,
    });
    activity(`已添加演示站点 ${domain}`, "site");
    persist();
    closeModal();
    navigate("sites");
    toast("站点已添加，请连接演示会话。");
    return;
  }
  if (form.id === "tags-form") {
    const a = findAsset(form.dataset.id);
    a.tags = [
      ...new Set(
        String(values.get("tags"))
          .split(/[,，]/)
          .map((t) => t.trim())
          .filter(Boolean),
      ),
    ].slice(0, 5);
    persist();
    renderPage();
    renderModal();
    toast("标签已保存。");
    return;
  }
  if (form.id === "rule-form") {
    const id = values.get("id"),
      existing = state.rules.find((r) => r.id === id);
    const name = String(values.get("name") || "").trim();
    if (!name) {
      toast("请输入规则名称。", true);
      return;
    }
    const rule = {
      id: id || uniqueId("rule"),
      name: name.slice(0, 60),
      siteId: values.get("siteId"),
      schedule: values.get("schedule"),
      action: values.get("action"),
      enabled: existing?.enabled ?? true,
    };
    if (existing) Object.assign(existing, rule);
    else state.rules.push(rule);
    activity(`${existing ? "更新" : "创建"}自动化演示规则：${name}`);
    persist();
    closeModal();
    renderPage();
    toast("规则配置已保存，原型不会在后台定时执行。");
    return;
  }
  if (form.id === "settings-form") {
    state.settings = {
      concurrency: Math.max(
        1,
        Math.min(5, Number(values.get("concurrency")) || 3),
      ),
      autoProcess: values.has("autoProcess"),
      preserveOriginal: true,
    };
    persist();
    renderPage();
    toast("设置已保存到当前浏览器。");
  }
});
document.addEventListener("keydown", (event) => {
  if (!state || event.isComposing || ui.modal?.type === "remote") return;
  if (ui.modal?.type === "palette" && event.target.id === "palette-q" && ["ArrowDown", "ArrowUp", "Enter"].includes(event.key)) {
    event.preventDefault();
    const items = paletteItems(ui.modal);
    if (event.key === "Enter") return runPaletteItem(ui.modal.index || 0);
    if (!items.length) return;
    ui.modal.index = ((ui.modal.index || 0) + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
    updatePaletteResults(ui.modal);
    document.querySelector(".palette-item.active")?.scrollIntoView({ block: "nearest" });
    return;
  }
  if (event.key === "Escape") {
    const menu = document.querySelector("details.menu[open]");
    if (menu) menu.removeAttribute("open");
    else if (ui.modal) closeModal();
    else if (document.body.classList.contains("nav-open")) setNavigationOpen(false);
    else if (ui.focusId && ["discover", "library"].includes(ui.page)) closeAssetInspector();
  }
  const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName) || document.activeElement?.isContentEditable;
  if (!ui.modal && !typing && !event.metaKey && !event.ctrlKey && !event.altKey && event.key.toLowerCase() === "n") {
    event.preventDefault();
    focusHomeIntake();
    return;
  }
  if (!ui.modal && !typing && !event.target.closest?.("video, audio") && ui.focusId && ["discover", "library"].includes(ui.page) && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
    event.preventDefault();
    stepInspector(event.key === "ArrowRight" ? 1 : -1);
    return;
  }
  if ((event.metaKey || event.ctrlKey) && event.key === "k") {
    event.preventDefault();
    if (ui.modal && ui.modal.type !== "palette") return;
    if (ui.modal?.type === "palette") closeModal();
    else openPalette();
  }
  if (
    (event.key === "Enter" || event.key === " ") &&
    event.target.classList.contains("asset-preview")
  ) {
    event.preventDefault();
    event.target.click();
  }
  if (event.key === "Tab" && ui.modal) {
    const items = [
      ...document.querySelectorAll(
        ".modal button:not(:disabled),.modal input:not(:disabled):not([type=hidden]),.modal select:not(:disabled),.modal a[href]",
      ),
    ].filter((e) => e.offsetParent !== null);
    if (!items.length) return;
    const first = items[0],
      last = items.at(-1);
    if (!document.querySelector(".modal").contains(document.activeElement)) {
      event.preventDefault();
      first.focus();
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
});
function scheduleLocalInfo(dialog) {
  clearTimeout(dialog.localTimer);
  if (ui.modal === dialog && dialog.localInfo?.active && !dialog.localBusy) {
    dialog.localTimer = setTimeout(() => readLocalAuth(dialog), 2500);
  }
}

async function readLocalAuth(dialog) {
  if (ui.modal !== dialog || dialog.localBusy) return;
  const revision = dialog.localRevision || 0;
  try {
    const info = await api.request(`/api/sites/${encodeURIComponent(dialog.id)}/local-auth`, {allowMissing: true});
    if (ui.modal !== dialog || revision !== (dialog.localRevision || 0)) return;
    const changed = dialog.localReadError || JSON.stringify(info) !== JSON.stringify(dialog.localInfo);
    dialog.localReadError = "";
    dialog.localInfo = info;
    if (changed) renderModal();
  } catch (error) {
    if (ui.modal === dialog && revision === (dialog.localRevision || 0)) {
      dialog.localReadError = error.message;
      renderModal();
    }
  } finally {
    if (ui.modal === dialog && revision === (dialog.localRevision || 0)) scheduleLocalInfo(dialog);
  }
}

async function manageLocalAuth(operation, id) {
  const dialog = ui.modal;
  if (dialog?.type !== "auth" || dialog.id !== id || dialog.localBusy) return;
  clearTimeout(dialog.localTimer);
  dialog.localRevision = (dialog.localRevision || 0) + 1;
  dialog.localBusy = operation;
  dialog.localNotice = dialog.localReadError = dialog.localError = "";
  renderModal();
  const endpoint = `/api/sites/${encodeURIComponent(id)}/local-auth`;
  try {
    if (operation === "save") {
      const saved = await api.request(endpoint + "/save", {method:"POST", body:{}});
      if (!saved?.saved) throw new Error("未收到会话保存结果，请检查本机浏览器后再试。");
      dialog.localInfo = {...dialog.localInfo, available:true, active:false};
      dialog.localNotice = "本机浏览器会话已保存，可重新拾取目标链接检查权限。";
      const site = findSite(id);
      if (site) site.sessionSaved = true;
      try { await refreshRealState(true); }
      catch (error) { dialog.localError = `会话已保存，工作区刷新失败。${error.message}`; }
    } else {
      dialog.localInfo = operation === "start"
        ? await api.request(endpoint, {method:"POST", body:{}})
        : await api.request(endpoint, {method:"DELETE"});
      if (operation === "start" && !dialog.localInfo?.active) {
        throw new Error(dialog.localInfo?.message || "本机浏览器尚未打开，请重试。");
      }
      if (operation === "cancel") dialog.localNotice = "已取消本次本机授权。";
    }
  } catch (error) {
    dialog.localError = error.message;
  } finally {
    dialog.localBusy = "";
    if (ui.modal === dialog) {
      renderModal();
      scheduleLocalInfo(dialog);
    }
  }
}

const remoteSession = createSessionPanel({
  api,
  root: $("#overlay-root"),
  onClose: closeModal,
  onLocalAuth: (id) => {
    closeModal();
    openModal("auth", {id, localInfo: {available:true, active:false}});
    void manageLocalAuth("start", id);
  },
  onSaved: async () => { await refreshRealState(); toast("会话已保存，权限待验证。可关闭窗口后发起扫描。"); },
});

async function boot() {
  $("#app").innerHTML = `<div class="service-state"><div class="service-state-content">${icon("refresh", 28)}<h1>正在连接工作空间</h1><p>检查服务状态并读取素材记录…</p></div></div>`;
  try {
    const mode = await api.mode();
    state = mode === "real" ? await api.state() : loadDemoState();
    if (!isReal()) { ui.homeScope = "column"; if (ui.page === "results") ui.page = "home"; }
    if (publicWorkspace(state)) {
      ui.homeScope = "single";
      if (workspacePage(state, ui.page) !== ui.page) { ui.page = "home"; history.replaceState(null, "", "#home"); }
    }
    serviceError = "";
    if (location.hash === "#discover") history.replaceState(null, "", "#home");
    renderShell();
    if (isReal() && ui.page === "results") void refreshResult(true);
    if (isReal() && ui.page === "settings") void refreshOperations();
    persist();
  } catch (error) {
    $("#app").innerHTML = `<div class="service-state"><div class="service-state-content">${icon("alertCircle", 28)}<h1>无法连接工作空间</h1><p>${esc(error.message)}</p><button class="btn btn-primary" data-action="retry-service">${icon("refresh", 16)}重试连接</button></div></div>`;
  }
}
function renderServiceWarning() {
  const status = $("[data-service-status]");
  if (isReal() && status) {
    const online = !serviceError;
    status.textContent = online ? state.publicMode === true ? "下载服务在线" : "本机服务在线" : "服务连接中断";
    status.closest(".service-chip").classList.toggle("is-real", online);
  }
  const element = $("#service-warning");
  if (element) element.innerHTML = serviceError ? `<div class="service-error-banner"><span>${esc(serviceError)} 当前显示最后一次成功读取的数据。</span><button class="btn btn-secondary" data-action="retry-state">重试</button></div>` : "";
}

// Replacing a pressed control before the browser dispatches click loses that click.
let workspacePress = null, releasePressTimer;
function clearWorkspacePress() {
  clearTimeout(releasePressTimer);
  workspacePress = null;
}
function holdWorkspacePress(target, token) {
  if (!target.closest?.("button, a[href], label, input, select, summary, [data-action]")) return;
  clearWorkspacePress();
  workspacePress = token;
}
function releaseWorkspacePress(token) {
  if (workspacePress !== token) return;
  // A release outside the original control may have no click to clear the guard.
  releasePressTimer = setTimeout(clearWorkspacePress, 400);
}
// The service answers missing thumbnails with the 64px app mark; show it as a quiet placeholder.
document.addEventListener("load", (event) => {
  const img = event.target;
  if (img?.tagName !== "IMG") return;
  const placeholder = /favicon\.svg$/.test(img.src) || (/\/thumbnail$/.test(img.src) && img.naturalWidth <= 64 && img.naturalHeight <= 64);
  img.classList.toggle("is-placeholder", placeholder);
}, true);
window.addEventListener("pointerdown", event => {
  if (event.isPrimary && event.button === 0) holdWorkspacePress(event.target, event.pointerId);
}, true);
window.addEventListener("pointerup", event => releaseWorkspacePress(event.pointerId), true);
window.addEventListener("pointercancel", event => {
  if (workspacePress === event.pointerId) clearWorkspacePress();
}, true);
window.addEventListener("keydown", event => {
  if (event.key === " " || event.key === "Enter") holdWorkspacePress(event.target, event.key);
}, true);
window.addEventListener("keyup", event => releaseWorkspacePress(event.key), true);
window.addEventListener("click", clearWorkspacePress);
window.addEventListener("blur", clearWorkspacePress);
window.addEventListener("pagehide", clearWorkspacePress);

function workspaceInteractionActive() {
  return Boolean(workspacePress !== null || ui.modal || (ui.page === "home" && ui.homeBusy) || ui.processingBusy || ui.operations.busy
    || document.querySelector("details.menu[open]")
    || ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)
    || $("[data-motion-editor]")
    || $('[data-region-toggle][aria-pressed="true"]')
    || [...document.querySelectorAll("video")].some(video => !video.paused && !video.ended));
}
function refreshProcessingJobs() {
  const jobs = $("[data-processing-jobs]");
  if (jobs) jobs.innerHTML = renderProcessingJobs(state);
  const count = $("[data-processing-job-count]");
  if (count) count.textContent = state.processingJobs.length;
}

async function refreshOperations({ refreshBackups = true, quiet = false } = {}) {
  const runtime = ui.operations;
  if (runtime.loading || runtime.busy) return;
  const generation = runtime.generation = (runtime.generation || 0) + 1;
  runtime.loading = true;
  if (!quiet && ui.page === "settings" && !workspaceInteractionActive()) renderPage();
  try {
    const results = await Promise.allSettled([
      api.request("/api/operations"),
      ...(refreshBackups ? [api.request("/api/maintenance/backups")] : []),
    ]);
    if (generation !== runtime.generation) return;
    if (results[0].status === "fulfilled") runtime.data = results[0].value;
    if (results[1]?.status === "fulfilled") runtime.backups = results[1].value.backups || [];
    runtime.loadError = results.filter(result => result.status === "rejected").map(result => result.reason.message).join(" ");
    runtime.error = [runtime.actionError, runtime.loadError].filter(Boolean).join(" ");
    runtime.updatedAt = Date.now();
  } finally {
    if (generation === runtime.generation) {
      runtime.loading = false;
      if (ui.page === "settings") {
        if (!workspaceInteractionActive()) renderPage();
        else ui.pendingRefresh = true;
      }
    }
  }
}

async function maintainBackup(id) {
  const runtime = ui.operations;
  if (runtime.busy) return;
  // Reads started before this action describe an obsolete backup snapshot.
  runtime.generation = (runtime.generation || 0) + 1;
  runtime.loading = false;
  runtime.busy = true;
  runtime.actionError = "";
  runtime.error = runtime.loadError || "";
  renderPage();
  try {
    const backup = await api.request(id ? `/api/maintenance/backups/${encodeURIComponent(id)}/verify` : "/api/maintenance/backups", { method: "POST", body: {} });
    runtime.backups = [backup, ...runtime.backups.filter(item => item.id !== backup.id)];
    toast(id ? "备份校验通过。" : "完整备份已创建并校验通过。");
  } catch (error) {
    runtime.actionError = error.message;
    runtime.error = [runtime.actionError, runtime.loadError].filter(Boolean).join(" ");
    throw error;
  } finally {
    runtime.busy = false;
    if (ui.page === "settings") renderPage();
    void refreshOperations({ refreshBackups: false, quiet: true });
  }
}
async function refreshResult(force = false) {
  const current = ui.result;
  if (ui.page !== "results" || !current.id || current.loading) return;
  const status = state.scans.find(scan => scan.id === current.id)?.status;
  if (state.intakeAvailable === false) {
    current.error = "此服务版本还不能读取拾取结果。重启更新后的本机服务，再重新连接。";
    if (force) renderPage();
    return;
  }
  const savedCount = state.assets.reduce((count, item) => count + Number(hasOriginal(item)), 0);
  if (!force && current.data && !current.error && current.savedCount === savedCount && current.scanCount === state.scans.length
      && current.data.status === status && !["queued","running"].includes(status)) return;
  current.loading = true;
  let changed = false;
  try {
    const result = await api.request(`/api/intake/${encodeURIComponent(current.id)}/results`);
    if (ui.result !== current) return;
    if (result.scanId !== current.id || !Array.isArray(result.groups)) throw new Error("服务返回的拾取结果无效。");
    changed = Boolean(current.error) || JSON.stringify(result) !== JSON.stringify(current.data);
    current.savedCount = savedCount;
    current.scanCount = state.scans.length;
    current.data = result;
    current.error = "";
  } catch (error) {
    changed = current.error !== error.message;
    current.error = error.message;
  } finally {
    current.loading = false;
    if (ui.result === current && ui.page === "results" && (changed || force)) {
      if (!workspaceInteractionActive()) renderPage();
      else ui.pendingRefresh = true;
    }
  }
}

async function continueIntake() {
  const current = ui.result;
  if (current.view.continuing || !current.data?.canContinue || ["queued","running","needs-user"].includes(current.data.status)) return;
  current.view.continuing = true;
  renderPage();
  try {
    const next = await api.request(`/api/intake/${encodeURIComponent(current.id)}/continue`, {method:"POST", body:{}});
    if (!resultRoute(`#results/${next?.id || ""}`) || next.id === current.id) throw new Error("服务未返回下一轮记录，请刷新后重试。");
    if (ui.page === "results" && ui.result === current && !ui.modal) navigate("results", next.id);
    await refreshRealState(true).catch(() => {});
  } finally {
    current.view.continuing = false;
    if (ui.page === "results" && ui.result === current) renderPage();
  }
}

function scopedResultItems(element) {
  const {group, episode, asset, selected} = element.dataset;
  const items = resultItems(ui.result.data, state, {group, episode}, selected ? ui.result.view.selected : null);
  return asset ? items.filter(item => item.id === asset) : items;
}

async function downloadIntakeSelection(element) {
  const view = ui.result.view;
  if (view.downloading) return;
  const reserved = reservedAssets(state);
  const ids = scopedResultItems(element).filter(asset => availableForDownload(asset, reserved)).map(asset => asset.id);
  if (!ids.length) throw new Error("所选素材已保存、已在队列，或需要权限。");
  view.downloading = true;
  renderPage();
  let added = 0, skipped = 0;
  try {
    for (let i = 0; i < ids.length; i += 1000) {
      const outcome = await api.request("/api/downloads", {method:"POST", body:{ids: ids.slice(i, i + 1000)}});
      added += outcome.added;
      skipped += outcome.duplicates + outcome.blocked;
    }
  } catch (error) {
    throw new Error(`已加入 ${added} 项。后续请求未完成：${error.message}`);
  } finally {
    view.downloading = false;
    await refreshRealState(true).catch(() => {});
  }
  toast(`已加入下载队列 ${added} 项${skipped ? `，跳过 ${skipped} 项` : ""}。`, false, {page:"queue", label:"查看队列"});
}

function exportIntakeSelection(element) {
  const {group, episode} = element.dataset;
  const ids = scopedResultItems(element).filter(hasOriginal).map(asset => asset.id);
  if (!ids.length) throw new Error("所选范围还没有已保存原件。");
  const payload = {ids, ...(group !== undefined ? {groupKey:group} : {}), ...(episode !== undefined ? {episodeKey:episode} : {})};
  submitArchiveDownload(`/api/intake/${encodeURIComponent(ui.result.id)}/archive-file`, payload, "导出拾取文件");
  toast(`已请求导出 ${ids.length} 项原件。`);
}

function submitArchiveDownload(path, payload, title) {
  // Native attachment downloads stream to disk; JS never assembles the ZIP in memory.
  const frame = document.createElement("iframe");
  frame.hidden = true;
  frame.name = uniqueId("archive");
  frame.title = title;
  frame.addEventListener("load", () => {
    const doc = frame.contentDocument;
    if (!doc || doc.URL === "about:blank") return;
    let message = "导出失败，请刷新记录后再试。";
    try { message = JSON.parse(doc.body.textContent).detail || message; } catch {}
    toast(message, true);
    frame.remove();
  });
  document.body.append(frame);
  const form = document.createElement("form");
  form.hidden = true;
  form.method = "POST";
  form.action = path;
  form.target = frame.name;
  const field = document.createElement("input");
  field.type = "hidden";
  field.name = "payload";
  field.value = JSON.stringify(payload);
  form.append(field);
  document.body.append(form);
  form.submit();
  form.remove();
}

async function refreshRealState(force = false) {
  const generation = stateReadGeneration;
  try {
    const next = await api.state();
    if (generation !== stateReadGeneration) return;
    const changed = JSON.stringify(next) !== JSON.stringify(state);
    state = next;
    if (ui.submittedSeriesScanId) {
      const scan = next.scans?.find(scan => scan.id === ui.submittedSeriesScanId);
      if (scan?.restoredSeriesId) {
        ui.submittedSeriesScanId = null;
        if (ui.page === "series") toast("已重新加入剧集库。");
      }
    }
    serviceError = "";
    renderServiceWarning();
    if (!$("#page-content")) return;
    updateNav();
    if (ui.page === "results") void refreshResult();
    const interactive = workspaceInteractionActive();
    if (!interactive && (force || changed || ui.pendingRefresh)) renderPage();
    else if (changed) {
      ui.pendingRefresh = true;
      if (ui.page === "process" && workspacePress === null) refreshProcessingJobs();
    }
  } catch (error) {
    if (generation !== stateReadGeneration) return;
    serviceError = error.message;
    renderServiceWarning();
    throw error;
  }
}
const scanStatusLabels = { queued: "等待扫描", running: "正在扫描", completed: "扫描完成", failed: "扫描失败", cancelled: "已取消", "needs-user": "等待站点验证", "timed-out": "扫描超时", "unsupported-drm": "媒体受 DRM 保护", "unsupported-media": "暂不支持此播放格式" };
function indexSeriesByScan(series) {
  const index = new Map();
  for (const item of series || []) {
    for (const id of item.sourceScanIds || [item.scanId]) index.set(id, item);
  }
  return index;
}
function scanDirectoryLink(scan, index) {
  if (scan.intakeScope || scan.mode === "site") return `<button class="btn btn-secondary" data-action="intake-open" data-id="${esc(scan.id)}">${intakeDestination(scan.intakeScope || scan.mode, scan.id).page === "series" ? "前往剧集" : "查看素材"}</button>`;
  const series = index.get(scan.id);
  if (!series || series.siteId !== scan.siteId || scan.seriesDismissed) return "";
  return `<button type="button" class="btn btn-secondary" data-action="manage-scan-series" data-id="${esc(series.id)}">管理此剧目录</button>`;
}
function renderScanHistory() {
  const index = indexSeriesByScan(state.series);
  const scans = state.scans || [];
  const page = scanRecordsPage(scans, ui.modal?.page, 10);
  return `${modalHeader("扫描历史", `共 ${scans.length} 次扫描`)}<div class="modal-body">${scans.length ? `<ol class="scan-history-list" start="${page.start + 1}">${page.rows.map(scan => {
    const date = new Date(scan.finishedAt || scan.createdAt);
    const when = Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", {hour12:false}) : "";
    const site = findSite(scan.siteId)?.name || "未知来源";
    const info = [site, scanStatusLabels[scan.status] || scan.status, when].filter(Boolean).join(" · ");
    return `<li class="scan-history-row"><div class="scan-history-info"><strong>${esc(scan.catalog?.title || scan.url || "扫描记录")}</strong><small>${esc(info)}</small></div>${scanDirectoryLink(scan, index)}</li>`;
  }).join("")}</ol>` : "<p>暂无扫描记录。</p>"}</div><div class="modal-footer scan-history-footer">${scanRecordsPager(page, "scan-history-page")}<button class="btn btn-secondary" data-action="close-modal">关闭</button></div>`;
}
function openScanSeries(id) {
  if (!state.series?.some(item => item.id === id)) {
    toast("该剧已从剧集库移除。", true);
    return;
  }
  if (ui.modal) closeModal();
  ui.seriesFilters = { query:"", siteId:"", category:"", page:1 };
  ui.seriesFilters.page = Math.floor((state.series || []).findIndex(item => item.id === id) / SERIES_PAGE_SIZE) + 1;
  ui.checkedSeries = [];
  delete ui.episodeFilters[id];
  delete ui.episodeSelection[id];
  ui.seriesExpanded[id] = true;
  navigate("series");
  const card = document.getElementById(`series-pick-${id}`)?.closest("[data-series-id]");
  const target = card?.querySelector("details[data-series-catalog] > summary") || card?.querySelector("[data-series-check]");
  target?.focus({preventScroll:true});
  target?.scrollIntoView({block:"center", behavior:"instant"});
}
function renderRealScans() {
  const index = indexSeriesByScan(state.series);
  const { active, results, needsAttention } = discoveryScanResults(index);
  if (!active.length && !results.length) return "";
  const open = ui.discoveryRecordsOpen;
  let content = "";
  if (open) {
    const rows = [...active.map(scan => ({scan})), ...results];
    const visible = rows.slice(0, ui.discoveryScanLimit);
    content = visible.map(row => row.series ? renderDiscoveryGroup(row, index) : renderDiscoveryScan(row.scan, index)).join("");
    const remaining = rows.length - visible.length;
    if (remaining) content += `<button type="button" class="btn btn-secondary" data-action="catalog-show-more">显示更多扫描结果（剩余 ${remaining} 项）</button>`;
  }
  return `<section class="scan-board scan-records" aria-label="扫描记录与剧集目录">
    <button type="button" class="scan-records-toggle" id="scan-records-toggle" data-action="catalog-toggle-records" aria-controls="scan-records-content" aria-expanded="${open}">
      ${icon("film", 16)}<strong>扫描记录与剧集目录</strong><span class="records-count">${results.length} 项结果</span>
      ${active.length ? `<span class="records-active">${active.length} 项进行中</span>` : ""}
      ${needsAttention ? `<span class="records-attention">${needsAttention} 项待处理</span>` : ""}
      ${icon("chevronDown", 15)}
    </button>
    <div class="scan-records-content" id="scan-records-content"${open ? "" : " hidden"}>${content}</div></section>`;
}
function scanRecordsPage(items, requested, size) {
  const pages = Math.max(1, Math.ceil(items.length / size));
  const current = Math.min(pages, Math.max(1, Math.trunc(Number(requested)) || 1));
  const start = (current - 1) * size;
  return {current, pages, start, rows:items.slice(start, start + size)};
}
function scanRecordsPager({current, pages}, action, id = "") {
  if (pages <= 1) return "";
  const button = (value, label) => `<button type="button" class="btn btn-secondary" id="${action}-${esc(id)}-${value < current ? "prev" : "next"}" data-action="${action}" data-id="${esc(id)}" data-value="${value}" ${value < 1 || value > pages ? "disabled" : ""}>${label}</button>`;
  return `<nav class="scan-records-pager" aria-label="扫描记录分页">${button(current - 1, "上一页")}<span aria-live="polite">第 ${current} / ${pages} 页</span>${button(current + 1, "下一页")}</nav>`;
}
function discoveryGroupSeries(scan, index) {
  const series = index.get(scan.id);
  if (series?.siteId === scan.siteId && series?.sourceScanIds?.length > 1) return series;
  if (scan.discoverySeriesId) {
    return {id:scan.discoverySeriesId, title:scan.customSeriesTitle || scan.catalog?.title || scan.catalog?.seriesKey, siteId:scan.siteId};
  }
  return null;
}
function discoveryScanResults(index = indexSeriesByScan(state.series)) {
  const active = [], results = [], groups = new Map(), recentUrls = new Set();
  let needsAttention = 0;
  for (const [position, scan] of (state.scans || []).entries()) {
    const key = JSON.stringify([scan.siteId, scan.mode, scan.url]);
    const latest = !recentUrls.has(key);
    recentUrls.add(key);
    if (["queued", "running"].includes(scan.status)) {
      active.push(scan);
      continue;
    }
    if (scan.discoveryCatalogDismissed) continue;
    const unresolved = latest && !scan.seriesNoticeDismissed && !scan.seriesDismissed
      && ["failed", "timed-out", "needs-user", "unsupported-drm", "unsupported-media"].includes(scan.status);
    if (unresolved) needsAttention++;
    if (scan.catalog?.entries?.length) {
      if (scan.discoveryDismissedEpisodeKeys?.length && !discoveryCatalog(scan)) continue;
      const series = discoveryGroupSeries(scan, index);
      if (series) {
        if (!groups.has(series.id)) {
          const group = {series, scans:[]};
          groups.set(series.id, group);
          results.push(group);
        }
        groups.get(series.id).scans.push(scan);
      } else {
        results.push({scan});
      }
      continue;
    }
    if (unresolved || (position < 3 && latest && !scan.seriesNoticeDismissed)) {
      results.push({scan});
    }
  }
  return { active, results, groups, needsAttention };
}
function renderDiscoveryGroup({series, scans}, index) {
  const expanded = ui.discoveryGroupOpen[series.id] === true;
  const id = esc(series.id);
  const site = findSite(series.siteId)?.name || series.siteId || "未知来源";
  const title = series.title || series.catalog?.seriesKey || "未命名剧集";
  const count = scans.length;
  let records = "";
  if (expanded) {
    const page = scanRecordsPage(scans, ui.discoveryGroupPages[series.id], 3);
    records = page.rows.map(scan => renderDiscoveryScan(scan, index)).join("") + scanRecordsPager(page, "catalog-group-page", series.id);
  }
  return `<section class="discovery-scan-group" aria-label="${esc(title)}的历次扫描"><header>
    <div><h3>${esc(title)}</h3><p>${esc(site)} · ${count} 次扫描目录</p></div>
    <div class="discovery-group-actions"><button type="button" class="btn btn-secondary" id="group-toggle-${id}" data-action="catalog-toggle-group" data-id="${id}" aria-controls="group-scans-${id}" aria-expanded="${expanded}">${expanded ? "收起历次扫描" : "查看历次扫描"}</button>
    <button type="button" class="btn btn-danger" data-action="catalog-dismiss-group" data-id="${id}">移除这部剧的全部扫描目录</button></div></header>
    <div class="discovery-group-records" id="group-scans-${id}" ${expanded ? "" : "hidden"}>${records}</div></section>`;
}
function renderDiscoveryScan(scan, index) {
  const running = ["queued", "running"].includes(scan.status);
  const needsUser = scan.status === "needs-user";
  const failed = ["failed", "timed-out", "unsupported-drm", "unsupported-media"].includes(scan.status);
  let glyph = "checkCircle";
  if (running) glyph = "refresh";
  else if (needsUser || failed) glyph = "alertCircle";
  else if (scan.status === "cancelled") glyph = "x";
  return `<div class="scan-progress ${failed || needsUser ? "is-error" : ""}">${icon(glyph, 17)}<span>${scanStatusLabels[scan.status] || esc(scan.status)} · 已读取 ${Number(scan.pagesVisited) || 0} 页 · 已发现 ${Number(scan.assetsFound) || 0} 项</span><small>${esc(scan.error || scan.message || scan.url)}</small>${running ? `<button class="btn btn-ghost" data-action="cancel-scan" data-id="${esc(scan.id)}">取消</button>` : ""}${needsUser && scan.siteId ? `<button class="btn btn-secondary" data-action="auth-site" data-id="${esc(scan.siteId)}">管理站点权限</button>` : ""}${scanDirectoryLink(scan, index)}</div>${renderEpisodeCatalog(scan, ui.discoverySelection[scan.id] || [], {
    archiveAvailable: state.seriesArchiveAvailable === true, discovery: true,
    open: ui.discoveryExpanded[scan.id] === true, filter: ui.discoveryFilters[scan.id] || {},
    siteName: findSite(scan.siteId)?.name,
  })}`;
}
function renderCatalogEmpty(library, all) {
  const plainFilters = (!ui.discoveryGroup || ui.discoveryGroup === "all") && ui.status === "all" && !ui.q.trim() && ui.site === "all" && ui.kind === "all" && ui.category === "all" && ui.content === "all";
  if (!library && all.length && plainFilters && ui.inbox === "pending") {
    return `<div class="empty-state is-calm"><div class="empty-filter-icon is-jade">${icon("check", 25)}</div><h3>收件箱已清空</h3><p>发现的素材都已下载，可以在资料库中查看原件。新的扫描结果会出现在这里。</p><div class="empty-actions"><button class="btn btn-primary" data-action="new-scan" data-value="column">${icon("plus", 15)}拾取新链接</button><button class="btn btn-secondary" data-action="navigate" data-page="library">打开资料库</button></div></div>`;
  }
  if (!library && !all.length && state.assets.length) {
    return `<div class="empty-state"><div class="empty-filter-icon">${icon("search", 25)}</div><h3>发现列表已清空</h3><p>扫描其他页面后，新发现的素材会出现在这里。</p><button class="btn btn-primary" data-action="new-scan" data-value="column">${icon("plus", 15)}拾取链接</button></div>`;
  }
  if (isReal() && !all.length) {
    const action = library ? 'data-action="navigate" data-page="discover"' : 'data-action="new-scan"';
    return `<div class="empty-state"><div class="empty-filter-icon">${icon(library ? "folder" : "globe", 25)}</div><h3>${library ? "还没有已下载文件" : "还没有发现素材"}</h3><p>${library ? "从总览拾取链接，在剧集页下载，完成后可在这里查看原件。" : "粘贴页面链接，即可扫描其中的图片和视频。"}</p><button class="btn btn-primary" ${action}>${library ? "前往总览拾取" : "扫描链接"}</button></div>`;
  }
  if (!library && all.length && plainFilters && ["local", "locked"].includes(ui.inbox)) {
    const title = ui.inbox === "local" ? "还没有已下载的素材" : "当前没有需权限的素材";
    return `<div class="empty-state"><h3>${title}</h3><button class="btn btn-secondary" data-action="inbox" data-value="all">查看全部素材</button></div>`;
  }
  return `<div class="empty-state"><div class="empty-filter-icon">${icon(library ? "folder" : "search", 25)}</div><h3>${library && !all.length ? "还没有处理完成的素材" : "没有找到匹配的素材"}</h3><p>${library && !all.length ? "先下载素材，再到工作室创建第一个成品。" : "试试其他关键词，或者清除筛选条件。"}</p><button class="btn btn-secondary" data-action="${library && !all.length ? "navigate" : "clear-filters"}" data-page="${state.publicMode === true ? "home" : "process"}">${library && !all.length ? state.publicMode === true ? "拾取链接" : "打开工作室" : "清除筛选"}</button></div>`;
}
function renderFacets(all) {
  const facet = (action, value, label, count, active, glyph = "") => `<button class="facet ${active ? "active" : ""}" data-action="${action}" data-value="${esc(value)}" aria-pressed="${active}">${glyph ? icon(glyph, 15) : ""}<span>${label}</span><small>${count}</small></button>`;
  const collections = state.publicMode === true
    ? [["all", "全部文件", "archive"], ["favorites", "收藏", "heart"]]
    : isReal()
    ? [["all", "全部文件", "archive"], ["processed", "有成品版本", "wand"], ["original", "仅原件", "image"], ["favorites", "收藏", "heart"]]
    : [["all", "全部文件", "archive"], ["favorites", "收藏", "heart"]];
  const count = (key) => key === "all" ? all.length : key === "favorites" ? all.filter((a) => a.favorite).length : all.filter((a) => matchesFileCollection(a, key)).length;
  const sites = state.sites.map((site) => [site, all.filter((a) => a.siteId === site.id).length]).filter(([, n]) => n);
  return `<div class="facet-group"><span class="facet-caption">视图</span>${collections.map(([key, label, glyph]) => facet("collection", key, label, count(key), ui.collection === key, glyph)).join("")}</div>
  <div class="facet-group"><span class="facet-caption">类型</span>${[["all", "全部类型"], ["video", "视频"], ["image", "图片"]].map(([key, label]) => facet("kind", key, label, key === "all" ? all.length : all.filter((a) => a.kind === key).length, ui.kind === key)).join("")}</div>
  ${sites.length ? `<div class="facet-group"><span class="facet-caption">来源</span>${facet("facet-site", "all", "全部来源", all.length, ui.site === "all")}${sites.map(([site, n]) => facet("facet-site", site.id, esc(site.name), n, ui.site === site.id)).join("")}</div>` : ""}`;
}
function inspectorMedia(asset, url) {
  if (!url) return `<div class="inspector-media"><img src="${esc(asset.thumbnail)}" alt="${esc(asset.title)}"></div>`;
  return `<div class="inspector-media">${asset.kind === "video" ? `<video src="${esc(url)}" controls preload="metadata" ${asset.thumbnail && !/favicon\.svg$/.test(asset.thumbnail) ? `poster="${esc(asset.thumbnail)}"` : ""} aria-label="${esc(asset.title)}"></video>` : `<img src="${esc(url)}" alt="${esc(asset.title)}">`}</div>`;
}
function renderAssetInspector(a, list = []) {
  const site = findSite(a.siteId);
  const index = list.findIndex((item) => item.id === a.id);
  const bar = `<div class="inspector-bar"><span class="inspector-count">${index >= 0 ? `${index + 1} / ${list.length}` : ""}</span><button class="icon-button" data-action="inspector-step" data-value="-1" aria-label="上一项" title="上一项 ←" ${index <= 0 ? "disabled" : ""}>${icon("chevronRight", 15).replace("<svg", '<svg style="transform:rotate(180deg)"')}</button><button class="icon-button" data-action="inspector-step" data-value="1" aria-label="下一项" title="下一项 →" ${index < 0 || index >= list.length - 1 ? "disabled" : ""}>${icon("chevronRight", 15)}</button><button class="icon-button" data-action="close-inspector" aria-label="关闭详情" title="关闭 Esc">${icon("x", 15)}</button></div>`;
  const library = ui.page === "library";
  const local = isReal() ? hasOriginal(a) : ["downloaded", "processed"].includes(a.status);
  const versions = isReal() ? processedVersions(a) : (a.versions || []);
  const presentation = isReal() && library ? libraryFilePresentation(a, ui.collection) : { versionId: "original" };
  const url = isReal() && local ? versionFileUrl(a, presentation.versionId) : null;
  const dims = a.width && a.height ? `${a.width}×${a.height}` : "";
  const meta = [a.kind === "video" ? "视频" : "图片", dims, a.kind === "video" ? a.duration : "", a.sizeMB ? formatSize(a.sizeMB) : ""].filter(Boolean).join(" · ");
  let state_ = ["已发现", "is-gray"];
  if (a.locked) state_ = [isReal() ? "需要站点权限" : "待解锁", "is-amber"];
  else if (local) state_ = [isReal() ? state.publicMode === true ? "可保存到本机" : "原件在本机" : a.status === "processed" ? "已处理" : "已下载", "is-jade"];
  const classification = isReal() && a.kind === "video" ? `<span class="chip is-gray">${esc(classificationLabel(a.classification))}</span>` : "";
  let primary;
  if (local && state.publicMode === true) primary = saveOriginalLink(a, true);
  else if (local) primary = `<button class="btn btn-primary btn-block" data-action="process-asset" data-id="${esc(a.id)}">${icon("wand", 16)}送去工作室</button>`;
  else if (a.locked) primary = isReal() ? `<button class="btn btn-primary btn-block" data-action="auth-site" data-id="${esc(a.siteId)}">${icon("key", 16)}管理站点权限</button>` : `<button class="btn btn-primary btn-block" data-action="unlock-one" data-id="${esc(a.id)}">${icon("lock", 16)}解锁内容 · ${a.price} 金币</button>`;
  else primary = `<button class="btn btn-primary btn-block" data-action="download-one" data-id="${esc(a.id)}">${icon("download", 16)}加入下载</button>`;
  const save = isReal() && local && state.publicMode !== true ? `<a class="btn btn-secondary" href="${versionFileUrl(a)}?download=true" download>${icon("download", 15)}保存原件</a>` : "";
  const source = isReal() && /^https?:\/\//i.test(a.pageUrl || "") ? `<a class="inspector-source" href="${esc(a.pageUrl)}" target="_blank" rel="noopener noreferrer">${esc(a.pageUrl.replace(/^https?:\/\//, ""))}${icon("arrowUpRight", 12)}</a>` : "";
  const timeline = state.publicMode !== true && ((isReal() && local) || (!isReal() && versions.length)) ? `<section class="inspector-versions"><h4>版本</h4><ol><li class="${presentation.versionId === "original" ? "current" : ""}"><span class="dot"></span><div><strong>原件</strong><small>${esc(a.date || "")}${a.sizeMB ? ` · ${formatSize(a.sizeMB)}` : ""}</small></div>${isReal() ? `<button class="icon-button" data-action="detail" data-id="${esc(a.id)}" data-version="original" aria-label="查看原件" title="查看">${icon("arrowUpRight", 14)}</button>` : ""}</li>${versions.map((v, i) => `<li class="${presentation.versionId === v.id ? "current" : ""}"><span class="dot"></span><div><strong>版本 ${i + 1} · ${esc(v.label || v.name || "处理成品")}</strong><small>${esc(v.createdAt ? String(v.createdAt).slice(0, 16).replace("T", " ") : "演示版本")}${v.sizeBytes ? ` · ${formatSize(Number(v.sizeBytes) / 1024 ** 2)}` : ""}</small></div>${isReal() ? `<a class="icon-button" href="${versionFileUrl(a, v.id)}?download=true" download aria-label="保存版本 ${i + 1}" title="保存">${icon("download", 14)}</a>` : ""}</li>`).join("")}</ol>${isReal() && versions.length ? `<button class="btn btn-ghost btn-block" data-action="detail" data-id="${esc(a.id)}" data-version="${esc(versions.at(-1).id)}" data-compare="true">${icon("columns", 14)}打开对比视图</button>` : ""}</section>` : "";
  return `<div class="inspector-body">${bar}${inspectorMedia(a, url)}<div class="inspector-head"><h3>${esc(a.title)}</h3><p>${esc(meta)}</p></div><div class="chip-row"><span class="chip ${state_[1]}">${state_[0]}</span>${classification}${a.favorite ? `<span class="chip is-gray">${icon("heart", 11)}收藏</span>` : ""}</div><dl class="inspector-facts"><div><dt>来源</dt><dd>${esc(site?.name || "未知来源")}</dd></div><div><dt>分类</dt><dd>${esc(a.category || "—")}</dd></div><div><dt>日期</dt><dd>${esc(a.date || "—")}</dd></div>${a.tags.length ? `<div><dt>标签</dt><dd class="tag-list">${a.tags.map((t) => `<span>${esc(t)}</span>`).join("")}</dd></div>` : ""}</dl>${source}${timeline}<div class="inspector-actions">${primary}<div class="inspector-actions-row">${save}<button class="btn btn-secondary" data-action="detail" data-id="${esc(a.id)}" data-version="${esc(presentation.versionId)}">${icon("arrowUpRight", 15)}完整详情</button><button class="icon-button ${a.favorite ? "is-favorite" : ""}" data-action="favorite" data-id="${esc(a.id)}" aria-label="${a.favorite ? "取消收藏" : "收藏"}">${icon("heart", 16)}</button></div></div></div>`;
}
function taskRow(kind, tone, title, progress, caption, meta = "") {
  const pct = Math.max(0, Math.min(100, Math.round(Number(progress) || 0)));
  return `<div class="task-row"><div class="task-row-top"><span class="kind-tag tone-${tone}">${kind}</span><strong>${esc(title)}</strong><span class="task-pct">${pct}%</span></div><span class="bar"><i class="tone-${tone}" style="width:${pct}%"></i></span><div class="task-row-foot"><span>${esc(caption)}</span>${meta ? `<span>${esc(meta)}</span>` : ""}</div></div>`;
}
function homeTasks() {
  const rows = [];
  for (const job of state.jobs.filter((item) => ["running", "queued", "paused"].includes(item.status)).slice(0, 4)) {
    const asset = findAsset(job.assetId);
    rows.push(taskRow(job.type === "process" ? "处理" : "下载", job.type === "process" ? "blue" : "jade", asset?.title || "下载任务", job.progress, isReal() ? downloadCaption(job) : job.status === "running" ? "模拟进度，不传输真实文件" : job.status === "queued" ? "等待空闲任务位" : "进度已保留"));
  }
  for (const job of (state.processingJobs || []).filter((item) => ["running", "queued", "paused"].includes(item.status)).slice(0, 3)) {
    rows.push(taskRow("处理", "blue", findAsset(job.assetId)?.title || "处理任务", job.progress, job.error || { queued: "等待 CPU 处理位", running: "正在生成新版本", paused: "已暂停，继续时从原件重新处理" }[job.status] || "处理任务"));
  }
  for (const scan of (state.scans || []).filter((item) => ["running", "queued"].includes(item.status)).slice(0, 2)) {
    rows.push(taskRow("扫描", "amber", scan.catalog?.title || scan.url, Math.min(95, (Number(scan.pagesVisited) || 0) / Math.max(1, Number(scan.limit) || 20) * 100), `已读取 ${Number(scan.pagesVisited) || 0} 页 · 已发现 ${Number(scan.assetsFound) || 0} 项`));
  }
  return rows;
}
function homeAgenda() {
  const items = [];
  const counts = inboxCounts();
  const sortable = homePendingAssets(state).allCount;
  if (sortable > 0) items.push({ tone: "ivory", glyph: "compass", title: `${sortable} 项其他素材待下载`, body: "未归属剧集的素材可在总览中查看、下载或移除。", attrs: 'data-action="home-pending-open"', cta: "查看素材" });
  if (isReal()) {
    const ready = (state.series || []).map((item) => [item, selectCatalogEpisodes(item.catalog || {}, "ready").length]).filter(([, n]) => n > 0);
    for (const [item, n] of ready.slice(0, 2)) {
      const title = item.title || item.catalog?.title || "未命名剧集";
      const saved = Number(item.catalog?.downloadedCount) || 0;
      items.push({ tone: "jade", glyph: "download", title: `《${title}》有 ${n} 集可以下载`, body: `已保存 ${saved} 集 · 下载前可选择清晰度与音轨`, attrs: `data-action="series-download-ready" data-id="${esc(item.id)}"`, cta: `下载 ${n} 集` });
    }
  }
  const blockedEpisodes = isReal() ? (state.series || []).reduce((total, item) => total + selectCatalogEpisodes(item.catalog || {}, "needs-user").length, 0) : 0;
  const blocked = [blockedEpisodes && `${blockedEpisodes} 集剧集`, counts.locked && `${counts.locked} 项素材`].filter(Boolean).join("、");
  if (blocked) items.push({ tone: "amber", glyph: "key", title: `${blocked}需要站点权限`, body: isReal() ? "打开对应站点，完成授权后可重新扫描。" : "查看演示站点的访问权限。", attrs: 'data-action="navigate" data-page="sites"', cta: "管理来源" });
  const failedJobs = (state.jobs || []).filter((job) => ["failed", "needs-user"].includes(job.status)).length + (state.processingJobs || []).filter((job) => job.status === "failed").length;
  const stuckScans = actionableScans().filter(scan => !["running", "queued"].includes(scan.status)).length;
  if (failedJobs + stuckScans) items.push({ tone: "coral", glyph: "alertCircle", title: `${failedJobs + stuckScans} 个任务没有完成`, body: "查看原因后，可以重试或重新扫描。", attrs: 'data-action="navigate" data-page="queue" data-filter="all"', cta: "去处理" });
  return items;
}
function renderHome() {
  const real = isReal();
  const local = state.assets.filter(isLocalAsset);
  const versionCount = state.assets.reduce((total, a) => total + (real ? processedVersions(a).length : (a.versions || []).length), 0);
  const work = activeWork();
  const size = local.reduce((total, a) => total + (Number(a.sizeMB) || 0), 0);
  const scopes = real ? intakeScopes : DEMO_SCOPES;
  const scope = scopes.find(([key]) => key === ui.homeScope) || scopes[2];
  const savedTime = asset => Date.parse(asset.downloadedAt || asset.date || "") || 0;
  const recent = local.sort((a, b) => savedTime(b) - savedTime(a)).slice(0, 8);
  const series = (state.series || []).slice(0, 4);
  const tasks = homeTasks();
  const agenda = homeAgenda();
  return `<section class="home">
  ${renderWorkspaceNotice(state)}
  <div class="home-hero">
    <div class="home-intake-heading"><div><h1>拾取链接</h1><p>${state.publicMode === true ? "粘贴链接，下载视频或图片。" : "整站按剧归档，视频与海报一起保存。"}</p></div><button class="btn btn-secondary" data-action="scan-history">${icon("clock",15)}扫描历史</button></div>
    <form class="home-intake" id="home-intake-form">
      <span class="home-intake-icon">${icon("link", 20)}</span>
      <label class="sr-only" for="home-url">链接</label>
      <input id="home-url" name="url" type="text" inputmode="url" autocomplete="off" spellcheck="false" placeholder="粘贴站点主页、剧集或短视频链接…" value="${esc(ui.homeDraft)}">
      <button type="submit" class="btn btn-primary btn-lg">${real && scope[0] === "single" ? "直接下载" : "开始拾取"}<kbd>↵</kbd></button>
      <p class="home-intake-error form-error" role="alert"></p>
    </form>
    <div class="home-scope">
      <div class="segmented" role="group" aria-label="扫描范围">${scopes.map(([key, label]) => `<button type="button" class="${key === scope[0] ? "active" : ""}" data-action="home-scope" data-value="${key}" ${key === "series" && !real ? "disabled" : ""} aria-pressed="${key === scope[0]}">${label}</button>`).join("")}</div>
      <span class="home-scope-hint">${esc(scope[2])}</span>
      <button type="button" class="btn btn-ghost" data-action="home-more">${icon("tune", 14)}更多选项</button>
    </div>
  </div>
  <div class="home-columns">
    <section class="panel agenda">
      <header class="panel-head"><h2>需要你处理${agenda.length ? `<span class="panel-count">${agenda.length}</span>` : ""}</h2></header>
      ${agenda.length ? `<div class="agenda-list">${agenda.map((item) => `<button class="agenda-item tone-${item.tone}" ${item.attrs}><span class="agenda-glyph">${icon(item.glyph, 17)}</span><span class="agenda-copy"><strong>${esc(item.title)}</strong><small>${esc(item.body)}</small></span><span class="agenda-cta">${esc(item.cta)}${icon("arrowRight", 13)}</span></button>`).join("")}</div>` : state.assets.length || (state.series || []).length ? `<div class="panel-empty is-calm"><span class="calm-mark">${icon("check", 18)}</span><p><strong>一切就绪</strong>没有等待分拣、权限或失败的任务。</p></div>` : `<div class="panel-empty is-calm"><span class="calm-mark">${icon("link", 17)}</span><p><strong>从第一条链接开始</strong>粘贴到上方输入框，选好范围后开始拾取。之后需要你确认的事都会列在这里。</p></div>`}
    </section>
    <section class="panel">
      <header class="panel-head"><h2><span class="live-dot ${work.total ? "is-live" : ""}"></span>正在进行${work.total ? `<span class="panel-count">${work.total}</span>` : ""}</h2><button class="btn btn-ghost" data-action="navigate" data-page="queue">全部活动 ${icon("arrowRight", 13)}</button></header>
      ${tasks.length ? tasks.join("") : `<div class="panel-empty">${icon("pulse", 22)}<p>${publicWorkspace(state) ? "没有进行中的任务。开始拾取或下载后，进度会实时显示在这里。" : "没有进行中的任务。加入下载或开始处理后，进度会实时显示在这里。"}</p></div>`}
    </section>
  </div>
  ${real || series.length ? `<section class="home-section">
    <header class="panel-head"><h2>剧集${(state.series || []).length ? `<small>${(state.series || []).length} 部</small>` : ""}</h2><button class="btn btn-ghost" data-action="navigate" data-page="series">全部剧集 ${icon("arrowRight", 13)}</button></header>
    ${series.length ? `<div class="series-rows">${series.map((item) => {
      const catalog = item.catalog || {};
      const ready = selectCatalogEpisodes(catalog, "ready").length;
      const blocked = selectCatalogEpisodes(catalog, "needs-user").length;
      const saved = Number(catalog.downloadedCount) || 0;
      const title = item.title || catalog.title || catalog.seriesKey || "未命名剧集";
      return `<button class="series-row" data-action="manage-scan-series" data-id="${esc(item.id)}">${seriesPoster(item, state, episodeThumbnails(item, state))}<span class="series-row-body"><span class="series-row-title"><strong>${esc(title)}</strong><small>${esc(findSite(item.siteId)?.name || "")}</small></span>${seriesStrip(item, 48)}<span class="series-row-meta"><span class="dot-label is-jade">已保存 ${saved}</span><span class="dot-label is-ivory">可下载 ${ready}</span>${blocked ? `<span class="dot-label is-amber">需权限 ${blocked}</span>` : ""}</span></span><span class="series-row-cta">打开${icon("arrowRight", 13)}</span></button>`;
    }).join("")}</div>` : `<div class="panel-empty is-wide">${icon("film", 22)}<p>在上方选择「整站」或「整部剧」，扫描结果会自动归入剧集页。</p></div>`}
  </section>` : ""}
  ${renderHomePending(state, ui.homePending)}
  <section class="home-section">
    <header class="panel-head"><h2>最近拾取<small>${local.length ? `${local.length} 份原件${size ? ` · ${formatSize(size)}` : ""}${versionCount ? ` · ${versionCount} 个成品` : ""}` : real ? state.publicMode === true ? "待保存原件" : "本机原件" : "演示素材"}</small></h2><button class="btn btn-ghost" data-action="navigate" data-page="library">打开资料库 ${icon("arrowRight", 13)}</button></header>
    ${recent.length ? `<div class="contact-sheet">${recent.map((a) => `<button class="frame-card" data-action="detail" data-id="${esc(a.id)}"><span class="frame-thumb"><img src="${esc(a.thumbnail)}" alt="" loading="lazy"><span class="frame-tc">${esc(a.kind === "video" ? a.duration || "视频" : a.width ? `${a.width}×${a.height}` : "图片")}</span></span><span class="frame-title">${esc(a.title)}</span><span class="frame-meta">${esc(shortSite(findSite(a.siteId)))}${a.sizeMB ? ` · ${formatSize(a.sizeMB)}` : ""}</span></button>`).join("")}</div>` : `<div class="panel-empty is-wide">${icon("archive", 22)}<p>下载完成的原件会按时间排在这里。</p></div>`}
  </section>
</section>`;
}
async function submitHomeIntake(form, values) {
  await submitIntake(form, values, ui.homeScope || "series");
}
function focusHomeIntake({url, scope} = {}) {
  if (ui.modal) closeModal();
  if (url !== undefined) ui.homeDraft = url;
  const requested = ({column:"site",page:"single"})[scope] || scope;
  const inferred = requested || (publicWorkspace(state) && !ui.homeDraft ? "single" : suggestScope(normalizeScanUrl(ui.homeDraft)));
  if (intakeScopes.some(([id]) => id === inferred)) ui.homeScope = inferred;
  ui.explicitHomeScope = Boolean(requested);
  navigate("home");
  $("#home-url")?.focus();
}
function openIntakeDestination(scan, scope = scan.intakeScope || scan.mode) {
  const target = intakeDestination(scope, scan.id);
  if (target.page === "series") {
    ui.seriesFilters = {query:"", siteId:scan.siteId || "", category:"", page:1};
    ui.submittedSeriesScanId = scan.id;
    ui.flashSeriesScanId = scan.id;
    ui.seriesAddOpen = false;
  }
  if (ui.modal) closeModal();
  navigate(target.page, target.scanId);
}
const DEMO_SCOPES = [
  ["page", "单页", "只读取这一页的图片和视频", "frame"],
  ["column", "栏目", "沿栏目翻页，汇总多页素材", "layers"],
  ["series", "整部剧", "读取页面可识别的选集目录", "film"],
];
function openIntake(options = {}) {
  const scopes = isReal() ? intakeScopes : DEMO_SCOPES;
  if (isReal()) options = { ...options, scope: ({column:"site",page:"single"})[options.scope] || options.scope };
  const known = key => scopes.some(([id]) => id === key);
  let scope = known(options.scope) ? options.scope : (isReal() && suggestScope(options.url)) || (known(ui.intakeScope) ? ui.intakeScope : isReal() ? "site" : "column");
  if (!isReal() && scope === "series") scope = "column";
  openModal("intake", { scope, explicitScope: known(options.scope), url: options.url || "", siteId: options.siteId || "", opts: {} });
}
function renderIntakeModal(m) {
  const real = isReal();
  const scopes = real ? intakeScopes : DEMO_SCOPES;
  const scope = scopes.find(([key]) => key === m.scope) || scopes[0];
  const o = m.opts[scope[0]] || {};
  const kinds = `<div class="intake-kinds" role="group" aria-label="素材类型"><label class="pill-check"><input type="checkbox" name="video" ${o.video === false ? "" : "checked"}><span>${icon("video", 13)}视频</span></label><label class="pill-check"><input type="checkbox" name="image" ${o.image === false ? "" : "checked"}><span>${icon("image", 13)}图片</span></label></div>`;
  const unit = (name, value, max) => `<span class="input-unit"><input class="input" name="${name}" type="number" min="1" max="${max}" value="${esc(value)}"><em>页</em></span>`;
  let options;
  if (scope[0] === "series") options = `<label class="intake-opt is-wide"><span>剧名（选填）</span><input class="input" name="customTitle" maxlength="100" placeholder="留空使用站点剧名" value="${esc(o.customTitle || "")}"></label><label class="intake-opt"><span>本轮最多读取</span>${unit("limit", o.limit || ui.seriesDraft.limit || 100, 1000)}</label>`;
  else if (scope[0] === "site") options = `<label class="intake-opt"><span>本轮最多读取</span>${unit("limit", o.limit || 100, 1000)}</label><div class="intake-opt"><span>素材类型</span>${kinds}</div>`;
  else if (scope[0] === "column") options = `<label class="intake-opt"><span>翻页深度</span><select class="select" name="depth">${[[1, "向下 1 层"], [3, "最多 3 层"], [5, "最多 5 层"]].map(([v, l]) => `<option value="${v}" ${String(o.depth || 1) === String(v) ? "selected" : ""}>${l}</option>`).join("")}</select></label><label class="intake-opt"><span>最多页面</span>${unit("limit", o.limit || 20, real ? 100 : 1000)}</label><div class="intake-opt"><span>素材类型</span>${kinds}</div>`;
  else options = `<div class="intake-opt"><span>素材类型</span>${kinds}</div>`;
  const seen = new Set();
  const recent = real ? (state.scans || []).filter(scan => {
    if (!/^https?:/i.test(scan.url || "") || seen.has(scan.url)) return false;
    seen.add(scan.url);
    return true;
  }).slice(0, 3) : [];
  return `<form id="intake-form" class="intake" novalidate>
    <div class="intake-top"><span class="intake-badge">${icon("link", 17)}</span><div><h2>拾取链接</h2><p>${real ? "选择整站、整部剧，或只读取当前作品。" : "选择要模拟拾取的范围。"}</p></div><button type="button" class="icon-button" data-action="close-modal" aria-label="关闭弹窗">${icon("x", 18)}</button></div>
    <label class="intake-field"><span class="sr-only">网址</span><input id="intake-url" name="url" type="text" inputmode="url" autocomplete="off" spellcheck="false" placeholder="https://…" value="${esc(m.url || "")}"><kbd>↵</kbd></label>
    <div class="intake-scopes" role="radiogroup" aria-label="拾取范围">${scopes.map(([key, label, desc, glyph]) => {
      const disabled = key === "series" && !real;
      return `<button type="button" role="radio" aria-checked="${key === scope[0]}" class="scope-card ${key === scope[0] ? "active" : ""}" data-action="intake-scope" data-value="${key}" ${disabled ? "disabled" : ""}><span class="scope-icon">${icon(glyph, 17)}</span><strong>${label}</strong><small>${disabled ? "需要启动本地服务" : desc}</small></button>`;
    }).join("")}</div>
    <div class="intake-options" data-scope="${scope[0]}">${options}</div>
    <p id="scan-error" class="form-error" role="alert"></p>
    ${recent.length ? `<div class="intake-recent"><span>最近</span>${recent.map((scan) => `<button type="button" class="recent-chip" data-action="intake-recent" data-value="${esc(scan.url)}" data-scope="${esc(scan.intakeScope || (scan.mode === "series" ? "series" : Number(scan.depth) > 0 ? "site" : "single"))}">${icon(scan.mode === "series" ? "film" : "link", 12)}<span>${esc(scan.catalog?.title || scan.url.replace(/^https?:\/\//, ""))}</span></button>`).join("")}</div>` : ""}
    <div class="intake-foot"><span class="hint">${icon("shield", 12)}${real ? state.publicMode === true ? "下载完成后请保存到本机" : "只访问你有权限的页面 · 原件保存在本机" : "演示模式 · 不会访问真实站点"}</span><button type="button" class="btn btn-secondary" data-action="close-modal">取消</button><button type="submit" class="btn btn-primary">${real && scope[0] === "single" ? "直接下载" : "开始拾取"}${icon("arrowRight", 15)}</button></div>
  </form>`;
}
async function submitIntake(form, values, scope, siteId = "") {
  const error = form.querySelector(".form-error");
  const fail = (message) => {
    if (!error?.isConnected) return toast(message, true);
    error.textContent = message;
    form.classList.remove("is-shaking");
    void form.offsetWidth;
    form.classList.add("is-shaking");
  };
  if (error) error.textContent = "";
  const url = normalizeScanUrl(values.get("url"));
  if (!url) return fail("请输入有效的 HTTP 或 HTTPS 网址。");
  const kinds = ["video", "image"].filter((kind) => !form.querySelector(`[name="${kind}"]`) || values.has(kind));
  if (!kinds.length) return fail("至少选择一种素材类型。");
  ui.intakeScope = scope;
  if (!isReal()) {
    if (scope === "series") return fail("整剧扫描需要启动本地服务。");
    if (ui.scan) return fail("已有扫描正在进行，请等待完成或先取消。");
    const demo = new FormData();
    demo.set("url", url);
    demo.set("depth", scope === "page" ? "1" : String(values.get("depth") || 1));
    demo.set("limit", scope === "page" ? "1" : String(values.get("limit") || 20));
    kinds.forEach((kind) => demo.set(kind, "on"));
    ui.homeDraft = "";
    startScan(demo);
    return;
  }
  if (state.intakeAvailable === false) return fail("本机服务需要更新。重启后重新连接，即可使用这三个拾取范围。");
  const button = form.querySelector('button[type="submit"]');
  if (ui.homeBusy) return;
  const originPage = ui.page;
  const originModal = ui.modal;
  ui.homeBusy = true;
  button?.classList.add("is-busy");
  if (button) button.disabled = true;
  try {
    const source = state.sites.find(site => site.id === siteId && new URL(site.url).origin === new URL(url).origin);
    const limit = scope === "single" ? 1 : Number(values.get("limit") || 100);
    if (!Number.isInteger(limit) || limit < 1 || limit > 1000) throw new Error("每轮读取页数请填写 1–1000 之间的整数。");
    const customTitle = scope === "series" ? String(values.get("customTitle") || "").trim() : "";
    const scan = await api.request("/api/intake", { method: "POST", body: {
      scope, url, kinds, limit, ...(scope === "single" ? {autoDownload:true} : {}), ...(scope === "site" ? {catalogOnly:true} : {}), ...(source ? {siteId: source.id} : {}), ...(customTitle ? {customTitle} : {}),
    }});
    if (!scan?.id) throw new Error("服务未返回拾取记录，请查看扫描历史。");
    if (normalizeScanUrl(ui.homeDraft) === url) { ui.homeDraft = ""; ui.explicitHomeScope = false; }
    if (ui.page === originPage && ui.modal === originModal) {
      openIntakeDestination(scan, scope);
    }
    let message = ["site", "series"].includes(scope) ? "已开始拾取，剧集会自动归档到剧集页。" : scan.downloadAfterScan === true ? "正在解析链接，完成后会自动下载。" : "已开始拾取，可在扫描历史中重开本次结果。";
    try {
      await refreshRealState(true);
    } catch {
      message += " 面板状态暂未刷新，可在连接恢复后查看活动。";
    }
    toast(message);
  } catch (err) {
    fail(err.message);
  } finally {
    ui.homeBusy = false;
    for (const control of document.querySelectorAll('#home-intake-form button[type="submit"], #intake-form button[type="submit"]')) {
      if (control?.isConnected) {
        control.disabled = false;
        control.classList.remove("is-busy");
      }
    }
  }
}
function openPalette() {
  setNavigationOpen(false);
  openModal("palette", { q: "", index: 0 });
}
const PALETTE_PAGES = [["home", "overview"], ["series", "film"], ["process", "wand"], ["library", "archive"], ["queue", "pulse"], ["sites", "globe"], ["automation", "zap"], ["settings", "tune"]];
function paletteItems(m) {
  const q = String(m.q || "").trim().toLowerCase();
  const match = (text) => !q || String(text || "").toLowerCase().includes(q);
  const items = [];
  const url = normalizeScanUrl(m.q || "");
  if (q && url && /^https?:|\./.test(q)) items.push({ group: "拾取", glyph: "link", title: "拾取这个链接", hint: url.replace(/^https?:\/\//, ""), run: () => focusHomeIntake({ url }) });
  const actions = [
    { title: "拾取链接", hint: "N", glyph: "plus", keys: "新建 扫描 拾取 scan", run: () => focusHomeIntake() },
    { title: document.documentElement.dataset.theme === "light" ? "切换到深色界面" : "切换到浅色界面", hint: "外观", glyph: document.documentElement.dataset.theme === "light" ? "moon" : "sun", keys: "主题 深色 浅色 theme", run: () => setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light") },
    { title: "查看活动记录", hint: "通知", glyph: "bell", keys: "历史 记录 history", run: () => openModal("history") },
    { title: "使用指南", hint: "帮助", glyph: "help", keys: "帮助 help", run: () => openModal("help") },
  ];
  items.push(...actions.filter((item) => match(item.title + " " + item.keys)).map((item) => ({ group: "操作", ...item })));
  items.push(...PALETTE_PAGES.filter(([id]) => workspacePage(state, id) === id && match(pages[id] + " " + id)).map(([id, glyph]) => ({ group: "前往", glyph, title: pages[id], hint: ui.page === id ? "当前" : "", run: () => navigate(id) })));
  if (q) {
    const assets = state.assets.filter((a) => !a.dismissedFromDiscovery || isLocalAsset(a)).filter((a) => match(`${a.title} ${a.tags.join(" ")} ${a.category}`)).slice(0, 6);
    items.push(...assets.map((a) => ({ group: "素材", thumb: a.thumbnail, title: a.title, hint: isLocalAsset(a) ? "资料库" : "待下载素材", run: () => revealAsset(a) })));
    const series = (state.series || []).filter((item) => match(`${item.title} ${item.sourceTitle || ""} ${item.catalog?.title || ""}`)).slice(0, 4);
    items.push(...series.map((item) => ({ group: "剧集", glyph: "film", title: item.title || item.catalog?.title || "未命名剧集", hint: `${item.catalog?.entries?.length || 0} 集`, run: () => openScanSeries(item.id) })));
  }
  return items;
}
function renderPaletteResults(m) {
  const items = paletteItems(m);
  if (!items.length) return `<p class="palette-empty">没有匹配「${esc(m.q)}」的页面、操作或素材</p>`;
  const index = Math.min(m.index || 0, items.length - 1);
  let group = "";
  return items.map((item, i) => {
    const head = item.group !== group ? `<div class="palette-group">${esc(item.group)}</div>` : "";
    group = item.group;
    const lead = item.thumb ? `<span class="palette-thumb"><img src="${esc(item.thumb)}" alt=""></span>` : `<span class="palette-glyph">${icon(item.glyph || "arrowRight", 15)}</span>`;
    return `${head}<button type="button" id="palette-option-${i}" class="palette-item ${i === index ? "active" : ""}" data-action="palette-run" data-value="${i}" role="option" aria-selected="${i === index}">${lead}<span class="palette-title">${esc(item.title)}</span>${item.hint ? `<span class="palette-hint">${esc(item.hint)}</span>` : ""}</button>`;
  }).join("");
}
function updatePaletteResults(m) {
  $("#palette-results").innerHTML = renderPaletteResults(m);
  const active = $("#palette-results .active");
  if (active) $("#palette-q").setAttribute("aria-activedescendant", active.id);
  else $("#palette-q").removeAttribute("aria-activedescendant");
}
function renderPalette(m) {
  return `<div class="palette"><div class="palette-field">${icon("search", 17)}<input id="palette-q" role="combobox" aria-label="搜索素材、剧集或操作" aria-expanded="true" aria-controls="palette-results" aria-autocomplete="list" aria-activedescendant="palette-option-0" type="text" autocomplete="off" spellcheck="false" placeholder="搜索素材、剧集、页面，或粘贴链接…" value="${esc(m.q || "")}"><button type="button" class="palette-close" aria-label="关闭命令面板" data-action="close-modal"><kbd>Esc</kbd>${icon("x", 16)}</button></div><div class="palette-results" id="palette-results" role="listbox" aria-label="搜索结果">${renderPaletteResults(m)}</div><div class="palette-foot"><span><kbd>↑</kbd><kbd>↓</kbd>选择</span><span><kbd>↵</kbd>打开</span></div></div>`;
}
function runPaletteItem(index) {
  if (ui.modal?.type !== "palette") return;
  const item = paletteItems(ui.modal)[index];
  if (!item) return;
  closeModal();
  item.run();
}
function revealLibraryGroup(assetId) {
  const group = catalogContents().groups.find(item => item.items.some(({asset}) => asset.id === assetId));
  if (!group) return;
  const index = group.items.findIndex(({asset}) => asset.id === assetId);
  const needed = (Math.floor(index / LIBRARY_PAGE_SIZE) + 1) * LIBRARY_PAGE_SIZE;
  ui.librarySections.set(group.id, {open: true, shown: Math.max(needed, ui.librarySections.get(group.id)?.shown || 0)});
}
function focusCatalogItem(list, index) {
  const asset = list[index];
  if (!asset) return false;
  if (ui.page === "discover") {
    const {size} = discoveryPageSlice(list);
    ui.discoveryPage = Math.floor(index / size) + 1;
  }
  if (ui.page === "library") revealLibraryGroup(asset.id);
  ui.inspectorEnter = !ui.focusId;
  ui.focusId = asset.id;
  renderPage();
  return true;
}
function revealAsset(asset) {
  const local = isLocalAsset(asset);
  if (!local) {
    if (ui.modal) closeModal();
    openModal("detail", {id:asset.id, version:"original", compare:false});
    return;
  }
  navigate(local ? "library" : "discover");
  if (!local) ui.inbox = "all";
  const list = catalogAssets();
  const index = list.findIndex(item => item.id === asset.id);
  if (index < 0) return;
  const compact = window.matchMedia("(max-width: 1180px)").matches;
  if (compact) {
    if (local) revealLibraryGroup(asset.id);
    else ui.discoveryPage = Math.floor(index / discoveryPageSlice(list).size) + 1;
    renderPage();
  } else {
    focusCatalogItem(list, index);
  }
  const trigger = $(`[data-asset="${CSS.escape(asset.id)}"] [data-action="focus-asset"]`);
  trigger?.focus({preventScroll: true});
  trigger?.scrollIntoView({block: "nearest"});
  if (compact) {
    openModal("detail", { id: asset.id, version: "original", compare: false });
  } else {
    $('[data-action="close-inspector"]')?.focus({preventScroll: true});
  }
}
function closeAssetInspector() {
  const previous = ui.focusId;
  ui.focusId = "";
  renderPage();
  $(`[data-asset="${CSS.escape(previous)}"] [data-action="focus-asset"]`)?.focus({preventScroll: true});
}
function stepInspector(delta) {
  const list = catalogAssets();
  const index = list.findIndex(item => item.id === ui.focusId);
  if (index < 0 || !focusCatalogItem(list, index + delta)) return;
  const button = $(`[data-action="inspector-step"][data-value="${delta}"]:enabled`) || $('[data-action="close-inspector"]');
  button?.focus({preventScroll: true});
  $(`[data-asset="${CSS.escape(ui.focusId)}"]`)?.scrollIntoView({block: "nearest"});
}
function renderActivitySide() {
  if (!isReal()) return "";
  const scans = actionableScans().slice(0, 5);
  const jobs = state.processingJobs || [];
  return `${state.publicMode === true ? "" : `<section class="activity-side-section"><header><h2>处理任务 <span data-processing-job-count>${jobs.length}</span></h2><button class="btn btn-ghost" data-action="navigate" data-page="process">工作室 ${icon("arrowRight", 13)}</button></header><div data-processing-jobs>${renderProcessingJobs(state)}</div></section>`}
  <section class="activity-side-section"><header><h2>扫描</h2><button class="btn btn-ghost" data-action="navigate" data-page="home">总览 ${icon("arrowRight", 13)}</button></header>${scans.length ? scans.map((scan) => `<div class="scan-mini ${!["queued", "running"].includes(scan.status) ? "is-warn" : ""}"><span class="kind-tag tone-${!["queued", "running"].includes(scan.status) ? "amber" : "jade"}">${esc(state.publicMode === true && scan.status === "needs-user" ? "源站限制访问" : scanStatusLabels[scan.status] || scan.status)}</span><strong>${esc(scan.catalog?.title || scan.url)}</strong><small>已读取 ${Number(scan.pagesVisited) || 0} 页 · 已发现 ${Number(scan.assetsFound) || 0} 项</small>${["queued", "running"].includes(scan.status) ? `<button class="btn btn-ghost" data-action="cancel-scan" data-id="${esc(scan.id)}">取消</button>` : scan.status === "needs-user" && scan.siteId && state.publicMode !== true ? `<button class="btn btn-ghost" data-action="auth-site" data-id="${esc(scan.siteId)}">管理站点权限</button>` : ""}</div>`).join("") : `<p class="side-empty">没有进行中或需要处理的扫描。</p>`}</section>`;
}
function actionableScans() {
  const seen = new Set();
  return (state.scans || []).filter(scan => {
    const key = JSON.stringify([scan.siteId, scan.mode, scan.url]);
    const newer = seen.has(key);
    seen.add(key);
    if (["queued", "running"].includes(scan.status)) return true;
    return !newer && !scan.seriesNoticeDismissed && !scan.discoveryCatalogDismissed && !scan.seriesDismissed
      && ["needs-user", "failed", "timed-out", "unsupported-drm", "unsupported-media"].includes(scan.status);
  });
}
function renderRealHelp() {
  if (publicWorkspace(state)) return `${modalHeader("临时下载工作区", "免注册，用当前浏览器继续自己的任务。")}<div class="modal-body"><div class="tour-list">${[
    ["粘贴链接", "在总览选择单集、整部剧或整站，读取你有权限访问的视频与图片。"],
    ["保存文件", "下载完成后，在资料库点「保存到本机」，或按剧导出视频与海报。"],
    ["站点会话", "需要账号时，可在来源保存自己的站点会话。部分站点的人机验证或登录方式可能无法在服务中完成。"],
    ["临时保存", `${Number(state.retentionHours) || 24} 小时未使用后清理。清除浏览器数据或换浏览器会开启新空间，请及时保存文件。`],
  ].map(([title, body], index) => `<div class="tour-step"><span>${index + 1}</span><div><h3>${title}</h3><p>${body}</p></div></div>`).join("")}</div></div><div class="modal-footer"><button class="btn btn-primary" data-action="close-modal">知道了</button></div>`;
  return `${modalHeader("本地媒体工作空间", "从真实站点发现素材，下载并保存在本机。")}<div class="modal-body"><div class="tour-list">${[
    ["扫描页面", "粘贴页面链接，选择扫描范围，即可查找图片和视频。"],
    ["站点授权", "遇到会员或私有内容时，在「来源」打开对应站点的授权窗口，登录后保存会话。"],
    ["下载与保存", "挑选素材加入真实下载队列，完成后在「资料库」中预览、播放和保存原件。"],
    ["水印与成品", "工作室支持 CPU 文字水印和固定区域去除，原件始终保留；成品可对比、按来源与标签整理并批量导出。复杂背景可能留痕，完成后请检查效果。"],
    ["运行维护", "设置中可检查磁盘并创建备份。在「自动化」为来源页面设置定时检查，查看新增和入队数量。"],
  ].map(([title, body], index) => `<div class="tour-step"><span>${index + 1}</span><div><h3>${title}</h3><p>${body}</p></div></div>`).join("")}</div></div><div class="modal-footer"><button class="btn btn-primary" data-action="close-modal">知道了</button></div>`;
}
function renderRealDetail(asset, { versionId = "original", compare = false } = {}) {
  const site = findSite(asset.siteId);
  const downloaded = hasOriginal(asset);
  const downloadJob = state.jobs.find((job) => job.assetId === asset.id && job.type === "download" && job.status === "completed");
  const source = asset.pageUrl || asset.sourceUrl || "";
  const sourceLink = /^https?:\/\//i.test(source) ? `<a class="real-source-link" href="${esc(source)}" target="_blank" rel="noopener noreferrer">查看来源页面 ${icon("arrowUpRight", 12)}</a>` : "";
  return `${modalHeader(esc(asset.title), `${esc(site?.name || "未知来源")} · ${asset.kind === "video" ? "视频" : "图片"}`)}${downloaded ? state.publicMode === true ? renderPublicPreview(asset) : renderVersionWorkspace(asset, { versionId, compare }) : `<div class="detail-preview real-media-preview"><img src="${esc(asset.thumbnail)}" alt="${esc(asset.title)}"><span class="detail-label">发现记录 · 下载完成后可查看原件</span></div>`}<div class="modal-body"><div class="row" style="justify-content:space-between;margin-bottom:20px"><span class="badge ${downloaded ? "badge-green" : "badge-gray"}">${downloaded ? "原件已下载" : asset.locked ? "需要站点权限" : "已发现，尚未下载"}</span>${sourceLink}</div><dl class="detail-grid"><div><dt>来源站点</dt><dd>${esc(site?.domain || "—")}</dd></div><div><dt>文件大小</dt><dd>${asset.sizeMB ? formatSize(asset.sizeMB) : "待下载确认"}</dd></div><div><dt>素材分类</dt><dd>${esc(asset.category)}</dd></div><div><dt>发现日期</dt><dd>${esc(asset.date || "—")}</dd></div></dl>${downloaded ? renderDownloadSelection(downloadJob?.downloadOptions, asset.downloadSelection) : ""}${renderClassification(asset)}<form id="tags-form" data-id="${esc(asset.id)}"><label for="detail-tags" class="field-caption">素材标签（逗号分隔）</label><div class="detail-tags"><input class="input" id="detail-tags" name="tags" value="${esc(asset.tags.join("，"))}" maxlength="120"><button class="btn btn-secondary" type="submit">保存标签</button><button class="btn btn-ghost" type="button" data-action="favorite" data-id="${esc(asset.id)}">${icon("heart", 15)}${asset.favorite ? "已收藏" : "收藏"}</button></div></form>${state.publicMode === true ? "" : renderVersionRecords(asset)}</div><div class="modal-footer"><span class="hint">${icon("shield", 12)}${downloaded ? state.publicMode === true ? "临时保存，请及时下载到本机" : "保留本机原始文件" : "仅下载你有权限访问的媒体"}</span><button class="btn btn-secondary" data-action="close-modal">关闭</button>${downloaded ? state.publicMode === true ? saveOriginalLink(asset) : `<button class="btn btn-primary" data-action="process-asset" data-id="${esc(asset.id)}">${icon("wand", 16)}创建处理版本</button>` : asset.locked ? `<button class="btn btn-primary" data-action="auth-site" data-id="${esc(asset.siteId)}">管理站点权限</button>` : `<button class="btn btn-primary" data-action="download-one" data-id="${esc(asset.id)}">${icon("download", 16)}加入下载队列</button>`}</div>`;
}
function selectedVisibleEpisodes(item) {
  const checked = new Set(ui.episodeSelection[item.id] || []);
  return filterEpisodeEntries(item.catalog, ui.episodeFilters[item.id])
    .filter(entry => checked.has(entry.episodeKey)).map(entry => entry.episodeKey);
}
function requestSeriesRemoval(item) {
  if (ui.page !== "series") return;
  const checked = new Set(ui.checkedSeries);
  const ids = item ? [item.id] : filterSeriesItems(state.series || [], ui.seriesFilters)
    .filter(series => checked.has(series.id)).map(series => series.id);
  const episodeKeys = item ? selectedVisibleEpisodes(item) : undefined;
  if (!ids.length || (item && !episodeKeys.length)) return;
  openModal("confirm", {
    title: item ? `移除《${item.title || "未命名剧集"}》选中的 ${episodeKeys.length} 集？` : `移除选中的 ${ids.length} 部剧？`,
    message: episodeKeys
      ? "所选单集将从目录移除，后续扫描会继续隐藏这些集。已保存的文件、任务和扫描历史会保留。"
      : "移除后，你可以在「添加剧集」重新扫描这部剧的链接，将它重新加入剧集库。已保存的文件、任务和扫描历史会保留。",
    confirmAction: "confirm-series-dismiss", confirmLabel: "确认移除",
    body: episodeKeys ? { ids, episodeKeys } : { ids },
  });
}
async function dismissSeriesSelection() {
  const modal = ui.modal;
  if (ui.page !== "series" || modal?.confirmAction !== "confirm-series-dismiss" || modal.busy) return;
  if (!await commitCatalogRemoval(modal, "/api/series/dismiss")) return;
  const { ids, episodeKeys } = modal.body;
  if (episodeKeys) {
    const removed = new Set(episodeKeys);
    ui.episodeSelection[ids[0]] = (ui.episodeSelection[ids[0]] || []).filter(key => !removed.has(key));
  } else {
    const removed = new Set(ids);
    ui.checkedSeries = ui.checkedSeries.filter(id => !removed.has(id));
    for (const id of ids) {
      delete ui.episodeFilters[id];
      delete ui.episodeSelection[id];
      delete ui.seriesExpanded[id];
    }
  }
  modal.busy = false;
  if (ui.modal === modal) closeModal();
  renderPage();
  toast(episodeKeys ? `已从剧集库移除 ${modal.result.dismissed} 集。` : `已移除 ${modal.result.dismissed} 部剧，可在「添加剧集」重新扫描。`);
}
async function commitCatalogRemoval(modal, path) {
  modal.busy = true;
  modal.error = "";
  renderModal();
  try {
    if (!modal.result) {
      modal.result = await api.request(path, { method: "POST", body: modal.body });
      stateReadGeneration++;
    }
    await refreshRealState(true);
  } catch (error) {
    modal.busy = false;
    modal.error = modal.result ? `移除已保存，读取最新目录失败：${error.message}` : error.message;
    if (modal.result) modal.confirmLabel = "重新读取目录";
    if (ui.modal === modal) renderModal();
    return false;
  }
  return true;
}
function requestScanNoticeDismissal(scanIds) {
  if (ui.page !== "series" || !scanIds.length) return;
  const count = scanIds.length;
  openModal("confirm", {
    title: count === 1 ? "清除这条扫描提示？" : `清除当前 ${count} 条扫描提示？`,
    message: "提示将从剧集库隐藏，扫描历史中仍可查看记录。",
    confirmAction: "confirm-series-notices", confirmLabel: "确认清除", body: {scanIds},
  });
}
async function dismissScanNotices() {
  const modal = ui.modal;
  if (ui.page !== "series" || modal?.confirmAction !== "confirm-series-notices" || modal.busy) return;
  if (!await commitCatalogRemoval(modal, "/api/series/scan-notices/dismiss")) return;
  modal.busy = false;
  if (ui.modal === modal) closeModal();
  renderPage();
  toast("扫描提示已清除。");
}
function discoveryEntries(id) {
  const scan = state.scans.find(item => item.id === id);
  return scan ? filterEpisodeEntries(discoveryCatalog(scan), ui.discoveryFilters[id]) : [];
}
function checkedDiscoveryKeys(id) {
  const checked = new Set(ui.discoverySelection[id] || []);
  return discoveryEntries(id).filter(entry => checked.has(entry.episodeKey)).map(entry => entry.episodeKey);
}
function requestDiscoveryGroupRemoval(id) {
  if (ui.page !== "discover") return;
  const group = discoveryScanResults().groups.get(id);
  if (!group) {
    renderPage();
    toast("扫描组已更新，请在当前列表选择要移除的目录。");
    return;
  }
  const scanIds = group.scans.map(scan => scan.id);
  const title = group.series.title || group.series.catalog?.seriesKey || "未命名剧集";
  const site = findSite(group.series.siteId)?.name || group.series.siteId || "未知来源";
  openModal("confirm", {
    title: `移除《${title}》的全部 ${scanIds.length} 次扫描目录？`,
    message: `将从发现移除${site}的这 ${scanIds.length} 次已结束扫描的目录。已保存的文件、下载任务、剧集库和扫描历史会保留。`,
    confirmAction:"confirm-scan-catalog-dismiss", confirmLabel:"确认移除", groupId:id, body:{scanIds},
  });
}
function requestScanCatalogRemoval(id, scope) {
  const scan = state.scans.find(item => item.id === id);
  const catalog = scan && discoveryCatalog(scan);
  if (ui.page !== "discover") return;
  if (!catalog) {
    renderPage();
    toast("扫描目录已更新，这张卡片已不在当前列表中。");
    return;
  }
  const title = scan.customSeriesTitle || catalog.title || catalog.seriesKey || "未命名剧集";
  const source = findSite(scan.siteId)?.name || scan.siteId || "未知来源";
  const episodeKeys = scope === "episodes" ? checkedDiscoveryKeys(id) : null;
  if (episodeKeys && !episodeKeys.length) {
    renderPage();
    toast(discoveryEntries(id).length ? "请先勾选要移除的单集。" : "当前筛选没有匹配的单集，请调整筛选条件。");
    return;
  }
  openModal("confirm", {
    title: episodeKeys ? `从《${title}》的本次扫描移除选中的 ${episodeKeys.length} 集？` : `移除《${title}》的整个扫描目录（${catalog.entries.length} 集）？`,
    message: `本次扫描（${source} · ${discoveryScanIdentity(scan)}）只清理发现中的这张卡片。其他扫描卡片、剧集库、下载任务和已保存文件均保留。`,
    confirmAction: "confirm-scan-catalog-dismiss", confirmLabel: "确认移除", id,
    groupId: discoveryGroupSeries(scan, indexSeriesByScan(state.series))?.id,
    body: episodeKeys ? {scope, episodeKeys} : {scope},
  });
}
async function dismissScanCatalogSelection() {
  const modal = ui.modal;
  if (ui.page !== "discover" || modal?.confirmAction !== "confirm-scan-catalog-dismiss" || modal.busy) return;
  const { scanIds, episodeKeys } = modal.body;
  const id = modal.id;
  const path = scanIds ? "/api/scans/discovery-dismiss-group" : `/api/scans/${encodeURIComponent(id)}/discovery-dismiss`;
  if (!await commitCatalogRemoval(modal, path)) return;
  const entireCard = Boolean(scanIds || modal.body.scope === "catalog" || modal.result.discoveryCatalogDismissed);
  if (entireCard) {
    for (const removedId of scanIds || [id]) {
      delete ui.discoverySelection[removedId];
      delete ui.discoveryFilters[removedId];
      delete ui.discoveryExpanded[removedId];
    }
    if (modal.groupId) ui.discoveryGroupOpen[modal.groupId] = false;
  } else {
    const removed = new Set(episodeKeys);
    ui.discoverySelection[id] = (ui.discoverySelection[id] || []).filter(key => !removed.has(key));
  }
  modal.busy = false;
  if (ui.modal === modal) closeModal();
  renderPage();
  let message = scanIds ? `已从发现移除 ${modal.result.dismissed} 次扫描目录。` :
    episodeKeys ? `已从本次扫描目录移除 ${modal.result.dismissed} 集。` : "已从发现移除本次扫描目录。";
  if (entireCard && modal.groupId) {
    const remaining = discoveryScanResults().groups.get(modal.groupId)?.scans.length;
    if (remaining) message += `这部剧还剩 ${remaining} 次扫描目录。`;
  }
  toast(message);
}
async function saveRuleMutation(path, options, deletedId) {
  const rule = await api.request(path, options);
  stateReadGeneration++;
  if (deletedId) {
    state.rules = state.rules.filter(item => item.id !== deletedId);
  } else {
    const index = state.rules.findIndex(item => item.id === rule.id);
    if (index < 0) state.rules.push(rule);
    else state.rules[index] = rule;
  }
  if (ui.modal) ui.pendingRefresh = true;
  else renderPage();
}

async function confirmRuleRemoval(id) {
  const dialog = ui.modal;
  if (dialog?.confirmAction !== "confirm-delete-rule" || dialog.id !== id || dialog.busy) return;
  dialog.busy = true;
  dialog.error = "";
  renderModal();
  try {
    await saveRuleMutation(`/api/rules/${encodeURIComponent(id)}`, { method: "DELETE" }, id);
  } catch (error) {
    dialog.busy = false;
    dialog.error = error.message;
    if (ui.modal === dialog) renderModal();
    return;
  }
  dialog.busy = false;
  if (ui.modal === dialog) closeModal();
  toast("自动化规则已删除。");
}

function requestProcessingJobDeletion(id) {
  const job = state.processingJobs.find(item => item.id === id);
  if (!job) return;
  const name = findAsset(job.assetId)?.title || "该素材";
  const active = ["running", "queued"].includes(job.status);
  openModal("confirm", {
    id, title: "删除处理任务？", confirmAction: "confirm-delete-processing-job",
    confirmLabel: active ? "停止并删除" : "删除任务",
    message: `将删除「${name}」的这条处理记录。${active ? "任务将停止。" : ""}原件和已生成的成品会保留在资料库。`,
  });
}

async function deleteProcessingJob(id) {
  const dialog = ui.modal;
  if (dialog?.confirmAction !== "confirm-delete-processing-job" || dialog.id !== id || dialog.busy) return;
  dialog.busy = true;
  dialog.error = "";
  renderModal();
  try {
    await api.request(`/api/processes/${encodeURIComponent(id)}/action`, { method: "POST", body: { action: "delete" } });
  } catch (error) {
    dialog.busy = false;
    dialog.error = error.message;
    if (ui.modal === dialog) renderModal();
    return;
  }
  stateReadGeneration++;
  state.processingJobs = state.processingJobs.filter(job => job.id !== id);
  dialog.busy = false;
  if (ui.modal === dialog) closeModal();
  refreshProcessingJobs();
  updateNav();
  toast("处理任务已删除。");
}

async function handleRealAction(action, element) {
  const { id, value } = element.dataset;
  const series = () => {
    const item = (state.series || []).find(item => item.id === id);
    if (!item) throw new Error("找不到此剧集目录，请刷新后重试。");
    return item;
  };
  const selectSeriesEpisodes = (mode) => {
    const entries = filterEpisodeEntries(series().catalog, ui.episodeFilters[id]);
    ui.episodeSelection[id] = selectCatalogEpisodes({ entries }, mode, ui.episodeSelection[id]);
    ui.seriesExpanded[id] = true;
    renderPage();
  };
  const downloadSeriesEpisodes = mode => {
    const item = series();
    const episodeKeys = mode ? selectCatalogEpisodes(item.catalog, mode) : selectedVisibleEpisodes(item);
    if (!episodeKeys.length) throw new Error("请先选择符合条件的剧集。");
    return openDownloadOptions({seriesId:id, episodeKeys});
  };
  const scanAndCheckEpisodes = async () => {
    if (ui.seriesCheckSubmitting) return;
    const body = seriesCheckBody(state, series(), ui.episodeSelection[id], ui.episodeFilters[id], ui.coinDrafts);
    ui.seriesCheckSubmitting = true;
    try {
      renderPage();
      const scan = await api.request(`/api/series/${encodeURIComponent(id)}/scan`, {
        method:"POST", body,
      });
      toast(`已提交 ${scan.checkTotal ?? body.episodeKeys.length} 集${body.autoUnlock ? "扫描与金币解锁" : "扫描与权限检查"}。`);
      await refreshRealState(true);
    } finally {
      ui.seriesCheckSubmitting = false;
      if (ui.page === "series") renderPage();
    }
  };
  const catalogAction = async (operation) => {
    const episodeKeys = checkedDiscoveryKeys(id);
    if (!episodeKeys.length) throw new Error("请先勾选剧集。");
    if (operation === "downloads") return openDownloadOptions({ scanId: id, episodeKeys: [...episodeKeys] });
    const result = await api.request(`/api/scans/${encodeURIComponent(id)}/${operation}`, { method: "POST", body: { episodeKeys } });
    await refreshRealState(true);
    toast(catalogScanMessage(result, episodeKeys.length));
  };
  const selectEpisodes = (mode) => {
    const entries = discoveryEntries(id);
    ui.discoverySelection[id] = selectCatalogEpisodes({entries}, mode, ui.discoverySelection[id]);
    ui.discoveryExpanded[id] = true;
    renderPage();
  };
  const resultView = ui.result?.view;
  const handlers = {
    "intake-refresh": () => refreshResult(true),
    "intake-continue": () => continueIntake(),
    "intake-open": () => {
      const scan = state.scans.find(scan => scan.id === id);
      if (scan) openIntakeDestination(scan);
    },
    "intake-read": () => focusHomeIntake({url:value, scope:element.dataset.scope}),
    "intake-group": () => {
      Object.assign(resultView, {groupKey:value, episodeKey:null, episodePage:1, mediaPage:1, episodeMediaPage:1});
      renderPage();
      if (window.matchMedia("(max-width: 650px)").matches) {
        const content = $(".intake-group");
        content?.focus({preventScroll: true});
        content?.scrollIntoView({block: "start"});
      }
    },
    "intake-episode": () => {
      const expanded = element.getAttribute("aria-expanded") === "true";
      Object.assign(resultView, {episodeKey: expanded ? "" : value, episodeMediaPage:1});
      renderPage();
    },
    "intake-kind": () => { Object.assign(resultView, {kind:value, mediaPage:1, episodeMediaPage:1}); renderPage(); },
    "intake-page": () => {
      const field = element.dataset.field;
      if (!["groupPage","episodePage","mediaPage","episodeMediaPage"].includes(field)) return;
      resultView[field] = Number(value);
      if (field === "groupPage") Object.assign(resultView, {groupKey:null, episodeKey:null, episodePage:1, mediaPage:1, episodeMediaPage:1});
      if (field === "episodePage") Object.assign(resultView, {episodeKey:null, episodeMediaPage:1});
      renderPage();
    },
    "intake-select": () => {
      for (const asset of scopedResultItems(element)) {
        if (!element.hasAttribute("data-group") || resultView.kind === "all" || asset.kind === resultView.kind) resultView.selected.add(asset.id);
      }
      renderPage();
    },
    "intake-clear": () => { resultView.selected.clear(); renderPage(); },
    "intake-download": () => downloadIntakeSelection(element),
    "intake-export": () => exportIntakeSelection(element),
    "intake-clean": () => {
      const asset = findAsset(id);
      if (!asset || !hasOriginal(asset)) throw new Error("请先下载此素材的原件。");
      receiveStudioAssets([id]);
      ui.processingDraft.options.mode = "remove";
      navigate("process");
    },
    "edit-rule": () => {
      const original = state.rules.find(rule => rule.id === id);
      if (original) openModal("rule", { id, original: { ...original, kinds: [...original.kinds] } });
    },
    "delete-rule": () => {
      const rule = state.rules.find(item => item.id === id);
      if (rule) openModal("confirm", {
        title: `删除“${rule.name}”？`, message: "删除后会停止这条规则的定时检查。已创建的任务和素材会保留。",
        confirmAction: "confirm-delete-rule", confirmLabel: "删除规则", id,
      });
    },
    "confirm-delete-rule": () => confirmRuleRemoval(id),
    "toggle-rule": async () => {
      const rule = state.rules.find(item => item.id === id);
      if (!rule) return;
      const enabled = !rule.enabled;
      await saveRuleMutation(`/api/rules/${encodeURIComponent(id)}`, { method: "PATCH", body: { enabled } });
      toast(enabled ? "定时执行已开启，可在规则中查看下次检查时间。" : "定时执行已暂停。");
    },
    "run-rule": async () => {
      await saveRuleMutation(`/api/rules/${encodeURIComponent(id)}/run`, { method: "POST" });
      toast("扫描已提交，结果将在这条规则内更新。");
    },
    "process-preview-asset": () => {
      ui.processingDraft.previewId = id;
      renderPage();
    },
    "process-chunlou-glyph": () => submitStudioRequest(glyphRequest(state, currentStudioBatch())),
    "start-process": async () => {
      if (ui.processingBusy) return;
      const batch = currentStudioBatch();
      const track = studioTrack();
      let options = ui.processingDraft.options;
      if (options.mode !== "add" && track?.active) {
        const assetId = $(".processing-media-frame")?.dataset.processingAsset;
        if (batch.selectedIds.length !== 1 || batch.selectedIds[0] !== assetId) throw new Error("请仅选择当前标记轨迹的视频。");
        if (track.dirty) throw new Error("请先记录当前画框的位置。");
        options = { ...options, region: track.region, motion: track.points };
      }
      await submitStudioRequest(processingRequest(state, batch.selectedIds, options));
    },
    "process-select-all": () => {
      const batch = currentStudioBatch();
      ui.processIds = batch.selectedIds.length === batch.items.length ? [] : batch.items.map(({asset}) => asset.id);
      renderPage();
    },
    "delete-processing-job": () => requestProcessingJobDeletion(id),
    "confirm-delete-processing-job": () => deleteProcessingJob(id),
    "processing-job-action": async () => {
      await api.request(`/api/processes/${encodeURIComponent(id)}/action`, { method: "POST", body: { action: value } });
      await refreshRealState(true);
    },
    "compare-version": () => {
      if (ui.modal?.type !== "detail") return;
      ui.modal.compare = !ui.modal.compare;
      renderModal();
    },
    "refresh-operations": () => refreshOperations(),
    "backup-create": () => maintainBackup(),
    "backup-verify": () => maintainBackup(id),
    "export-originals": () => exportLibraryArchive("original"),
    "export-processed": () => exportLibraryArchive("latest"),
    "series-rename": () => {
      const item = series();
      openModal("series-rename", {id, customTitle:item.customTitle || "", sourceTitle:item.sourceTitle || item.title});
    },
    "series-check-visible": () => {
      const ids = seriesPageItems(state.series || [], ui.seriesFilters).items.map(item => item.id);
      const checked = new Set(ui.checkedSeries);
      if (ids.every(seriesId => checked.has(seriesId))) ids.forEach(id => checked.delete(id));
      else ids.forEach(id => checked.add(id));
      ui.checkedSeries = [...checked];
      renderPage();
    },
    "series-clear-filters": () => {
      ui.seriesFilters = { query: "", siteId: "", category: "", page: 1 };
      ui.checkedSeries = [];
      renderPage();
    },
    "series-page": () => {
      ui.seriesFilters.page = Number(value);
      renderPage();
      $(".series-library-tools")?.scrollIntoView({block:"start"});
    },
    "series-continue-site": async () => {
      element.disabled = true;
      try {
        const scan = await api.request(`/api/intake/${encodeURIComponent(id)}/continue`, {method:"POST",body:{}});
        openIntakeDestination(scan, "site");
        await refreshRealState(true);
        toast("已继续读取整站目录，新的剧集会合并到这里。");
      } finally { if (element.isConnected) element.disabled = false; }
    },
    "series-download-all": async () => {
      if (ui.seriesDownloadSubmitting.has(id)) return;
      ui.seriesDownloadSubmitting.add(id);
      renderPage();
      try {
        const result = await api.request(`/api/series/${encodeURIComponent(id)}/downloads`, {method:"POST", body:{resolveMissing:true}});
        await refreshRealState(true);
        toast(result.resolving ? "正在扫描并检查本剧，随后自动下载可用单集和海报。" : catalogDownloadMessage(result));
      } finally {
        ui.seriesDownloadSubmitting.delete(id);
        renderPage();
      }
    },
    "series-clear-episode-filters": () => {
      delete ui.episodeFilters[id];
      delete ui.episodeSelection[id];
      ui.seriesExpanded[id] = true;
      renderPage();
    },
    "series-notice-close": async () => {
      element.disabled = true;
      try {
        await api.request("/api/series/scan-notices/dismiss", {method:"POST", body:{scanIds:[id]}});
        const scan = (state.scans || []).find(item => item.id === id);
        if (scan) scan.seriesNoticeDismissed = true;
        renderPage();
      } finally { if (element.isConnected) element.disabled = false; }
    },
    "series-notice-dismiss": () => requestScanNoticeDismissal([id]),
    "series-notices-dismiss": () => requestScanNoticeDismissal(JSON.parse(element.dataset.ids)),
    "confirm-series-notices": dismissScanNotices,
    "series-notice-retry": () => {
      const scan = (state.scans || []).find(item => item.id === id);
      if (!scan || ui.page !== "series") return;
      ui.seriesAddOpen = true;
      ui.seriesDraft = { ...ui.seriesDraft, url: scan.url, siteId: scan.siteId || "", customTitle: scan.customSeriesTitle || "" };
      renderPage();
      $("#series-url")?.focus();
    },
    "manage-scan-series": () => openScanSeries(id),
    "series-dismiss-checked": () => requestSeriesRemoval(),
    "series-dismiss-episodes": () => requestSeriesRemoval(series()),
    "confirm-series-dismiss": dismissSeriesSelection,
    "series-select": () => selectSeriesEpisodes("all"),
    "series-select-pending": () => selectSeriesEpisodes("pending"),
    "series-select-ready": () => selectSeriesEpisodes("ready"),
    "series-scan-check": scanAndCheckEpisodes,
    "series-download-selected": () => downloadSeriesEpisodes(),
    "series-download-ready": () => downloadSeriesEpisodes("ready"),
    "series-rescan": async () => {
      const item = series();
      await api.request("/api/series", { method: "POST", body: { url: item.url, siteId: item.siteId, limit: Number(ui.seriesDraft.limit) || 100 } });
      await refreshRealState(true);
      toast("已提交目录扫描，新发现的集数会合并到这部剧。");
    },
    "catalog-toggle-group": () => {
      ui.discoveryGroupOpen[id] = !ui.discoveryGroupOpen[id];
      renderPage();
    },
    "catalog-toggle-records": () => {
      ui.discoveryRecordsOpen = !ui.discoveryRecordsOpen;
      renderPage();
    },
    "catalog-group-page": () => {
      ui.discoveryGroupPages[id] = Number(value);
      renderPage();
    },
    "scan-history-page": () => {
      if (ui.modal?.type !== "scan-history") return;
      ui.modal.page = Number(value);
      renderModal();
    },
    "catalog-dismiss-group": () => requestDiscoveryGroupRemoval(id),
    "catalog-show-more": () => {
      ui.discoveryScanLimit += 10;
      renderPage();
    },
    "catalog-clear-filters": () => {
      delete ui.discoveryFilters[id];
      delete ui.discoverySelection[id];
      ui.discoveryExpanded[id] = true;
      renderPage();
    },
    "catalog-dismiss-all": () => requestScanCatalogRemoval(id, "catalog"),
    "catalog-dismiss-episodes": () => requestScanCatalogRemoval(id, "episodes"),
    "confirm-scan-catalog-dismiss": dismissScanCatalogSelection,
    "catalog-select": () => selectEpisodes("all"),
    "catalog-select-pending": () => selectEpisodes("pending"),
    "catalog-select-ready": () => selectEpisodes("ready"),
    "catalog-scan": () => catalogAction("episodes"),
    "catalog-download": () => catalogAction("downloads"),
    "recover-job": async () => {
      const job = state.jobs.find(item => item.id === id);
      const asset = job && findAsset(job.assetId);
      if (!asset?.pageUrl) throw new Error("缺少原页面，请重新扫描站点。");
      await api.request("/api/scans", { method: "POST", body: {siteId: asset.siteId, url: asset.pageUrl, depth: 0, limit: 1, kinds: [asset.kind]} });
      await refreshRealState(true);
      navigate("discover");
      toast("已提交原页面扫描；完成后可返回队列重试。");
    },
    "check-local-auth": () => ui.modal?.type === "auth" && readLocalAuth(ui.modal),
    "start-local-auth": () => manageLocalAuth("start", id),
    "save-local-auth": () => manageLocalAuth("save", id),
    "cancel-local-auth": () => manageLocalAuth("cancel", id),
    "connect-site": async () => {
      const site = findSite(id);
      if (!site) throw new Error("站点不存在，请刷新后重试。");
      if (isLataSite(site) && ui.modal?.type === "auth") {
        await readLocalAuth(ui.modal);
        if (ui.modal?.localInfo?.available) {
          await manageLocalAuth("start", id);
          return;
        }
      }
      closeModal();
      lastFocus = element;
      ui.modal = { type: "remote", id };
      document.body.style.overflow = "hidden";
      await remoteSession.open(site, {publicMode:publicWorkspace(state)});
    },
    "disconnect-site": async () => {
      await api.request(`/api/sites/${encodeURIComponent(id)}/session`, { method: "DELETE" });
      await refreshRealState(true);
      if (ui.modal?.type === "auth") renderModal();
      toast("已清除本地保存的站点会话。");
    },
    "cancel-scan": async () => {
      await api.request(`/api/scans/${encodeURIComponent(id)}/cancel`, { method: "POST" });
      await refreshRealState(true);
      toast("已请求取消扫描。");
    },
    "download-selection": () => openDownloadOptions({ ids: [...ui.selected] }),
    "download-one": () => openDownloadOptions({ ids: [id] }),
    "retry-download-options": () => loadDownloadOptions(),
    "download-default": () => confirmDownloadOptions({ maxHeight: null, audioLanguage: "auto" }),
    "retry-download-selection": () => {
      const job = state.jobs.find((item) => item.id === id);
      if (!job || !["failed", "needs-user", "cancelled"].includes(job.status)) throw new Error("当前任务不可更改选项，请刷新队列。");
      return openDownloadOptions({ ids: [job.assetId], jobId: job.id });
    },
    "job-action": async () => {
      await api.request(`/api/jobs/${encodeURIComponent(id)}/action`, { method: "POST", body: { action: value } });
      await refreshRealState(true);
    },
    "pause-all": async () => {
      await api.request("/api/jobs/pause", { method: "POST" });
      await refreshRealState(true);
      toast("已请求暂停下载任务。");
    },
    favorite: async () => {
      const asset = findAsset(id);
      if (!asset) return;
      await api.request(`/api/assets/${encodeURIComponent(id)}`, { method: "PATCH", body: { favorite: !asset.favorite } });
      await refreshRealState(true);
      if (ui.modal?.type === "detail") renderModal();
    },
  };
  const unsupported = ["reset-demo", "confirm-reset", "confirm-unlock"];
  if (unsupported.includes(action)) { toast("此功能尚未接入真实服务。", true); return true; }
  if (!handlers[action]) return false;
  try {
    const result = handlers[action]();
    if (result && typeof result.then === "function") {
      element.disabled = true;
      await result;
    }
  }
  catch (error) { toast(error.message, true); }
  finally { if (element.isConnected) element.disabled = false; }
  return true;
}
async function submitRealForm(form, values) {
  const button = form.querySelector('button[type="submit"],button:not([type])');
  if (button?.disabled) return;
  if (button) button.disabled = true;
  try {
    if (form.id === "rule-form") {
      const dialog = ui.modal;
      if (dialog?.type !== "rule" || dialog.busy) return;
      const draft = ruleRequest(values, state.sites);
      const body = dialog.id ? changedRuleFields(dialog.original, draft) : draft;
      if (!Object.keys(body).length) { closeModal(); return; }
      dialog.busy = true;
      form.querySelector("#rule-error").textContent = "";
      try {
        const path = dialog.id ? `/api/rules/${encodeURIComponent(dialog.id)}` : "/api/rules";
        await saveRuleMutation(path, { method: dialog.id ? "PATCH" : "POST", body });
      } finally { dialog.busy = false; }
      if (ui.modal === dialog) closeModal();
      toast(dialog.id ? "规则修改已保存。" : draft.enabled ? "规则已保存，已安排下次检查。" : "规则已保存，定时执行已暂停。");
    } else if (form.id === "series-rename-form") {
      const dialog = ui.modal;
      if (dialog?.type !== "series-rename") return;
      await api.request(`/api/series/${encodeURIComponent(dialog.id)}/title`, {
        method: "PUT", body: {title: String(values.get("title") || "").trim()},
      });
      stateReadGeneration++;
      await refreshRealState(true);
      if (ui.modal === dialog) closeModal();
      toast("剧名已保存。");
    } else if (form.id === "series-scan-form") {
      const url = normalizeScanUrl(values.get("url"));
      if (!url) throw new Error("请输入有效的 HTTP 或 HTTPS 网址。");
      const limit = Number(values.get("limit"));
      if (!Number.isInteger(limit) || limit < 1 || limit > 1000) throw new Error("每轮读取页数请填写 1–1000 之间的整数。");
      const siteId = String(values.get("siteId") || "") || undefined;
      const customTitle = String(values.get("customTitle") || "").trim();
      const scan = await api.request("/api/series", { method: "POST", body: { url, siteId, limit, ...(customTitle ? {customTitle} : {}) } });
      registerSeriesScan(scan.id);
      ui.seriesDraft = { url: "", siteId: siteId || "", limit };
      if (form.contains(document.activeElement)) document.activeElement.blur();
      toast("已提交整剧扫描。");
      await refreshRealState(true);
    } else if (form.id === "scan-form") {
      const url = normalizeScanUrl(values.get("url"));
      if (!url) throw new Error("请输入有效的 HTTP 或 HTTPS 网址。");
      const kinds = ["image", "video"].filter((kind) => values.has(kind));
      if (!kinds.length) throw new Error("至少选择一种素材类型。");
      const siteId = ui.modal?.siteId || state.sites.find((site) => site.domain === new URL(url).hostname)?.id;
      await api.request("/api/scans", { method: "POST", body: { url, siteId, depth: Number(values.get("depth")), limit: Number(values.get("limit")), kinds } });
      closeModal();
      await refreshRealState(true);
      navigate("discover");
      toast("扫描任务已提交，进度来自本地服务。");
    } else if (form.id === "add-site-form") {
      const url = normalizeSiteUrl(values.get("url"));
      if (!url) throw new Error("请输入有效的 HTTP 或 HTTPS 网址。");
      await api.request("/api/sites", { method: "POST", body: { name: String(values.get("name") || "").trim(), url } });
      closeModal();
      await refreshRealState(true);
      navigate("sites");
      toast("站点已添加，可以开始扫描。");
    } else if (form.id === "download-options-form") {
      const modal = ui.modal;
      if (modal?.type !== "download-options" || modal.status !== "ready") return;
      modal.values = Object.fromEntries(values);
      await confirmDownloadOptions(parseDownloadOptions(modal.values, modal));
    } else if (form.id === "classification-form") {
      await api.request(`/api/assets/${encodeURIComponent(form.dataset.id)}`, { method: "PATCH", body: { classification: values.get("classification") } });
      await refreshRealState();
      renderModal();
      toast("内容分类已保存。");
    } else if (form.id === "tags-form") {
      const tags = [...new Set(String(values.get("tags") || "").split(/[,，]/).map((tag) => tag.trim()).filter(Boolean))].slice(0, 5);
      await api.request(`/api/assets/${encodeURIComponent(form.dataset.id)}`, { method: "PATCH", body: { tags } });
      await refreshRealState();
      renderModal();
      toast("标签已保存。");
    } else if (form.id === "settings-form") {
      await api.request("/api/settings", { method: "PATCH", body: { concurrency: Number(values.get("concurrency")) } });
      await refreshRealState(true);
      toast("下载并发设置已保存到本地服务。");
    } else { toast("此功能尚未接入真实服务。", true); }
  } catch (error) {
    if (form.id === "rule-form" && form.isConnected) form.querySelector("#rule-error").textContent = error.message;
    else if (form.id === "series-rename-form" && form.isConnected) $("#series-rename-error").textContent = error.message;
    else if (form.id === "series-scan-form" && $("#series-scan-error")) $("#series-scan-error").textContent = error.message;
    else if (form.id === "scan-form" && $("#scan-error")) $("#scan-error").textContent = error.message;
    else if (form.id === "download-options-form" && $("#download-options-error")) $("#download-options-error").textContent = error.message;
    else toast(error.message, true);
  } finally { if (button?.isConnected) button.disabled = false; }
}

function registerSeriesScan(id) {
  ui.submittedSeriesScanId = id;
  if (ui.seriesFilters.query || ui.seriesFilters.siteId) {
    ui.seriesFilters = { query: "", siteId: "", category: "", page: 1 };
    ui.checkedSeries = [];
  }
  stateReadGeneration++;
}

setInterval(() => {
  if (document.hidden || !state) return;
  if (isReal()) {
    if (ui.operations.busy) return;
    if (!polling) {
      polling = true;
      refreshRealState().catch(() => {}).finally(() => { polling = false; });
    }
    if (ui.page === "settings" && Date.now() - ui.operations.updatedAt > 8000) {
      void refreshOperations({ refreshBackups: false, quiet: true });
    }
    return;
  }
  const hasWork = state.jobs.some((job) =>
    ["running", "queued"].includes(job.status),
  );
  if (hasWork) {
    const before = state.jobs.filter(
      (job) => job.status === "completed",
    ).length;
    state = tickJobs(state);
    persist();
    updateNav();
    const completed = state.jobs.filter(
      (job) => job.status === "completed",
    ).length;
    if (completed > before) {
      ui.pendingRefresh = true;
      toast("模拟任务已完成，原始素材已保留。");
    }
  }
  const editing = ["INPUT", "SELECT", "TEXTAREA"].includes(
    document.activeElement?.tagName,
  );
  if (
    !ui.modal &&
    !editing &&
    (ui.pendingRefresh || (hasWork && ui.page === "queue"))
  )
    renderPage();
}, 1500);
boot();

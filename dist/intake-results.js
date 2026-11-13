import { escapeHtml as esc, icon } from "./ui.js";
import { hasOriginal, processedVersions, versionFileUrl } from "./version-view.js";

export const intakeScopes = [
  ["site", "整站", "从站点主页查找剧集，按剧归类素材", "layers"],
  ["series", "整部剧", "从详情页或任一集展开选集和素材", "film"],
  ["single", "单集或短视频", "只读取链接指向的这一集或作品", "play"],
];

const EPISODE_UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;

export function suggestScope(value) {
  let url;
  try { url = new URL(value); } catch { return ""; }
  if (!["https:", "http:"].includes(url.protocol)) return "";
  if (!url.port && !url.username && !url.password) {
    const id = "[A-Za-z0-9_-]{11}";
    if (url.hostname === "youtu.be" && new RegExp(`^/${id}/?$`).test(url.pathname)) return "single";
    if (["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"].includes(url.hostname)
        && ((url.pathname === "/watch" && url.searchParams.getAll("v").length === 1
          && new RegExp(`^${id}$`).test(url.searchParams.get("v")))
          || new RegExp(`^/(?:shorts|embed|live)/${id}/?$`).test(url.pathname))) return "single";
  }
  const route = url.hash.startsWith("#/") ? url.hash.slice(1) : url.pathname;
  if (/^\/?(?:\?.*)?$/.test(route)) return "site";
  if (url.protocol === "https:" && ["lata.casa", "www.lata.casa"].includes(url.hostname)
      && !url.port && !url.username && !url.password && route === url.pathname) {
    if (/^\/series\/?$/.test(route)) return "site";
    if (/^\/originals\/[A-Za-z0-9_-]{1,180}\/?$/.test(route)) return "series";
  }
  if (url.protocol === "https:" && ["chunlou.tv", "www.chunlou.tv"].includes(url.hostname)
      && !url.port && !url.username && !url.password && route === url.pathname) {
    if (/^\/feed\/?$/.test(route)) {
      const ids = url.searchParams.getAll("ep");
      if (ids.length === 1 && EPISODE_UUID.test(ids[0])) return "single";
      const slugs = url.searchParams.getAll("d");
      if (!ids.length && slugs.length === 1 && /^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/.test(slugs[0])) return "series";
    }
    if (/^\/drama\/[A-Za-z0-9][A-Za-z0-9_-]{0,99}\/?$/.test(route)) return "series";
  }
  if (/(^|\.)(?:tiktok\.com|douyin\.com|iesdouyin\.com|instagram\.com|instagr\.am)$/i.test(url.hostname)) return "single";
  if (/^\/s\/[^/]+\/[1-9][0-9]*\/?$/i.test(route)) return "single";
  if (/\/(?:watch|episode|episodes|video|reel|p)\/[^/]/i.test(route) || /\.(?:mp4|webm|m3u8)(?:$|[?#])/i.test(route)) return "single";
  if (/\/(?:series|show|shows|season|s)\/[^/]/i.test(route)) return "series";
  return "";
}

export function intakeResultIds(result, target = {}) {
  const unique = new Set();
  for (const group of result?.groups || []) {
    if (target.group !== undefined && group.key !== target.group) continue;
    if (target.episode === undefined) for (const id of group.assetIds || []) unique.add(id);
    for (const episode of group.episodes || []) {
      if (target.episode !== undefined && episode.key !== target.episode) continue;
      for (const id of episode.assetIds || []) unique.add(id);
    }
  }
  return [...unique];
}

export function resultRoute(hash) {
  return /^#results\/([A-Za-z0-9_-]{1,128})$/.exec(hash)?.[1] || "";
}

export function resultItems(result, state, target = {}, selected) {
  const assets = new Map(state.assets.map(asset => [asset.id, asset]));
  return intakeResultIds(result, target)
    .filter(id => !selected || selected.has(id))
    .map(id => assets.get(id)).filter(Boolean);
}

export function freshResultView() {
  return {groupKey: null, episodeKey: null, groupPage: 1, episodePage: 1, mediaPage: 1,
    kind: "all", query: "", category: "", episodeMediaPage: 1, selected: new Set(), downloading: false, continuing: false};
}

function pageRows(items, requested, perPage) {
  const pages = Math.max(1, Math.ceil(items.length / perPage));
  const current = Math.min(pages, Math.max(1, Number(requested) || 1));
  return {current, pages, rows: items.slice((current - 1) * perPage, current * perPage)};
}

function pager(name, data) {
  if (data.pages < 2) return "";
  const button = (value, label, off) => `<button class="btn btn-ghost" data-action="intake-page" data-field="${name}" data-value="${value}" ${off ? "disabled" : ""}>${label}</button>`;
  return `<nav class="result-paging" aria-label="分页">${button(data.current - 1, "上一页", data.current === 1)}<span>${data.current} / ${data.pages}</span>${button(data.current + 1, "下一页", data.current === data.pages)}</nav>`;
}

const selectTarget = (group, episode) => `${group === undefined ? "" : ` data-group="${esc(group)}"`}${episode === undefined ? "" : ` data-episode="${esc(episode)}"`}`;
const statusLabels = {queued:"等待扫描", running:"正在扫描", completed:"已完成", failed:"扫描失败", cancelled:"已取消", "needs-user":"需要验证", "timed-out":"已超时", "unsupported-drm":"DRM 保护", "unsupported-media":"暂不支持此播放格式"};
const watermarkLabels = {"source-clean":"源站无水印版本", playback:"源站播放版本 · 水印待核实", embedded:"画面带水印", unknown:"水印待核实"};
const platforms = {douyin:"抖音", tiktok:"TikTok", instagram:"Instagram", youtube:"YouTube"};
const pendingJobs = new Set(["running", "queued", "paused", "needs-user"]);
export function availableForDownload(asset, reserved) {
  return !hasOriginal(asset) && !asset.locked && !asset.unsupported && !reserved.has(asset.id);
}
export const reservedAssets = state => new Set((state.jobs || []).filter(job => pendingJobs.has(job.status)).map(job => job.assetId));

function counts(items) {
  const videos = items.filter(a => a.kind === "video").length;
  return `${videos} 视频 · ${items.length - videos} 图片`;
}

function batch(items, view, reserved, attrs = "", scopeLabel = "所选") {
  const download = items.filter(a => availableForDownload(a, reserved)).length;
  const saved = items.filter(hasOriginal).length;
  return `<div class="intake-result-actions"><button type="button" class="btn btn-secondary" data-action="intake-download"${attrs} ${!download || view.downloading ? "disabled" : ""}>${icon("download", 13)}下载${scopeLabel}${download ? ` · ${download}` : ""}</button><button type="button" class="btn btn-secondary" data-action="intake-export"${attrs} ${!saved ? "disabled" : ""}>${icon("archive", 13)}导出${scopeLabel}${saved ? ` · ${saved}` : ""}</button></div>`;
}

function assetStatus(asset, view, reserved) {
  if (hasOriginal(asset)) return "已保存";
  const job = view.jobs.get(asset.id);
  if (job?.status === "running") {
    const progress = Math.round(Math.min(100, Math.max(0, Number(job.progress) || 0)));
    return `下载中 ${progress}%`;
  }
  const labels = {queued:"等待下载", paused:"下载已暂停", "needs-user":"源站限制访问", failed:"下载失败"};
  if (labels[job?.status]) return labels[job.status];
  if (reserved.has(asset.id)) return "已在队列";
  if (asset.locked) return "需权限";
  return asset.unsupported ? "暂不支持" : "待下载";
}

function assetRow(asset, view, reserved) {
  const id = esc(asset.id);
  const saved = hasOriginal(asset);
  const short = asset.shortMedia;
  const status = assetStatus(asset, view, reserved);
  const label = asset.kind === "image" ? "图片" : "视频";
  const meta = [label, ...(short ? [platforms[short.platform] || short.platform, watermarkLabels[short.watermark] || watermarkLabels.unknown] : []), status].filter(Boolean).join(" · ");
  const latestRemoval = short && processedVersions(asset).filter(v => ["remove", "both"].includes(v.options?.mode || v.mode)).at(-1);
  return `<div class="intake-media-row" data-result-asset="${id}"><label class="intake-media-choice"><input type="checkbox" data-result-check="${id}" aria-label="选择 ${esc(asset.title || label)}" ${view.selected.has(asset.id) ? "checked" : ""}></label>
    <button type="button" class="intake-media-name" data-action="detail" data-id="${id}"><span class="intake-thumb">${asset.thumbnail ? `<img src="${esc(asset.thumbnail)}" alt="" loading="lazy">` : icon(asset.kind, 16)}</span><span><strong>${esc(asset.title || label)}</strong><small>${esc(meta)}</small></span></button>
    <div class="intake-result-actions">${saved ? `<a class="btn btn-secondary" href="${versionFileUrl(asset)}?download=true" download>${view.publicMode ? "保存到本机" : "导出原件"}</a>` : `<button type="button" class="btn btn-secondary" data-action="intake-download" data-asset="${id}" ${!view.downloading && availableForDownload(asset,reserved) ? "" : "disabled"}>${short?.watermark === "source-clean" ? "下载无水印版本" : "下载"}</button>`}
      ${!view.publicMode && short && saved && short.watermark !== "source-clean" ? `<button type="button" class="btn btn-ghost" data-action="intake-clean" data-id="${id}">选区去水印</button>` : ""}
      ${!view.publicMode && latestRemoval ? `<a class="btn btn-secondary" href="${versionFileUrl(asset,latestRemoval.id)}?download=true" download>导出去水印版本</a>` : ""}
    </div></div>`;
}

function mediaList(items, view, reserved, field) {
  const filtered = items.filter(a => view.kind === "all" || a.kind === view.kind);
  if (!filtered.length) return items.length ? '<p class="intake-muted">没有符合当前类型的素材。</p>' : "";
  const data = pageRows(filtered, view[field], 12);
  return `<div class="intake-media-list">${data.rows.map(asset => assetRow(asset, view, reserved)).join("")}</div>${pager(field,data)}`;
}
function readLink(url, scope, label) {
  if (!url) return "";
  return `<button type="button" class="btn btn-ghost" data-action="intake-read" data-value="${esc(url)}" data-scope="${scope}">${label}${icon("arrowRight",13)}</button>`;
}

function groupContent(group, result, view, assets, reserved, groupItems) {
  const attrs = selectTarget(group.key);
  const episodes = group.episodes || [];
  const episodePage = pageRows(episodes, view.episodePage, 12);
  const root = (group.assetIds || []).map(id => assets.get(id)).filter(Boolean);
  const site = result.scope === "site";
  const single = result.scope === "single";
  const scopeLabel = single ? "全部" : "本剧";
  const kindSelector = ["all","video","image"].map((k,i) => `<button class="${view.kind === k ? "active" : ""}" data-action="intake-kind" data-value="${k}" aria-pressed="${view.kind === k}">${["全部","视频","图片"][i]}</button>`).join("");
  return `<section class="intake-group" aria-label="素材目录" tabindex="-1"><header class="intake-group-head"><div><h2>${esc(group.title || (single ? "本条内容" : "剧集素材"))}</h2><small>${episodes.length ? `已列出 ${episodes.length} 集 · ` : ""}${counts(groupItems)}</small></div>${site ? readLink(group.url,"series","读取整部剧") : ""}${groupItems.length ? batch(groupItems, view, reserved, attrs, scopeLabel) : ""}</header>
      ${group.localOnlyIds?.length ? `<p class="intake-group-note">含 ${group.localOnlyIds.length} 份此前已保存的原件。</p>` : ""}
      ${group.message ? `<p class="intake-group-note">${esc(group.message)}</p>` : ""}
      ${groupItems.length ? `<div class="intake-type-filter"><div class="segmented" role="group" aria-label="素材类型">${kindSelector}</div><button class="btn btn-ghost" data-action="intake-select"${attrs}>选择当前类型</button></div>` : ""}
      ${root.length ? `${episodes.length ? "<h3>剧内素材</h3>" : ""}${mediaList(root, view, reserved, "mediaPage")}` : ""}
      ${episodes.length ? `<div class="intake-episodes"><h3>选集目录 <small>${episodes.length}</small></h3>${episodePage.rows.map(ep => {
        const items = (ep.assetIds || []).map(id => assets.get(id)).filter(Boolean);
        const opened = view.episodeKey === ep.key || (view.episodeKey === null && episodes.length === 1);
        const title = ep.title || (ep.ordinal ? `第 ${ep.ordinal} 集` : "单集");
        const ea = selectTarget(group.key, ep.key);
        const access = ep.access === "needs-user" ? (ep.accessEvidence === "public-directory" && ep.scanStatus !== "needs-user" ? "权限待核实" : "需权限") : "";
        const outcome = ["unsupported-media","unsupported-drm","failed"].includes(ep.mediaStatus) ? statusLabels[ep.mediaStatus] : "";
        const summary = [outcome || access, items.length ? counts(items) : ""].filter(Boolean).join(" · ") || "待解析";
        const emptyMessage = access ? "可在剧集目录复查本集权限。" : "当前类型下暂无素材。";
        return `<section class="intake-episode"><header><button class="intake-episode-toggle" aria-expanded="${opened}" data-action="intake-episode" data-value="${esc(ep.key)}">${icon(opened ? "chevronDown" : "chevronRight", 14)}<strong>${esc(title)}</strong><small>${esc(summary)}</small></button>${readLink(ep.url,"single",items.length || outcome ? "重新读取本集" : "读取本集")}${items.length ? batch(items,view,reserved,ea,"本集") : ""}</header>${opened ? `${ep.scanMessage ? `<p class="intake-muted">${esc(ep.scanMessage)}</p>` : ""}${mediaList(items, view, reserved, "episodeMediaPage") || (!ep.scanMessage ? `<p class="intake-muted">${emptyMessage}</p>` : "")}` : ""}</section>`;
      }).join("")}${pager("episodePage",episodePage)}</div>` : ""}
      ${!episodes.length && !root.length ? '<p class="intake-muted">此页面暂未列出可下载素材。</p>' : ""}
    </section>`;
}

function roundActions(result, view) {
  const link = (id, label) => /^[A-Za-z0-9_-]{1,128}$/.test(id || "")
    ? `<a class="btn btn-secondary" href="#results/${esc(id)}">${label}</a>` : "";
  const canContinue = result.canContinue === true && !result.nextScanId && ["site","series"].includes(result.scope)
    && !["queued","running","needs-user"].includes(result.status);
  return link(result.previousScanId, "查看上一轮") + link(result.nextScanId, "查看下一轮") +
    (canContinue ? `<button type="button" class="btn btn-primary" data-action="intake-continue" ${view.continuing ? "disabled" : ""}>${icon("arrowRight", 14)}${view.continuing ? "正在提交…" : "继续拾取"}</button>` : "");
}

function directDownloadStatus(result, state, items, jobs) {
  const scan = state.scans?.find(scan => scan.id === result.scanId);
  if (result.scope !== "single" || !(result.downloadAfterScan === true || scan?.downloadAfterScan === true)) return null;
  if (["queued", "running"].includes(result.status)) {
    return {label:"解析中", message:"解析完成后自动下载。" + (result.message || ""), active:true};
  }
  if (result.status !== "completed") return null;
  const saved = items.filter(hasOriginal).length;
  const current = items.filter(item => !hasOriginal(item)).map(item => jobs.get(item.id)).filter(Boolean);
  const hasJobs = items.some(item => jobs.has(item.id));
  if (items.length && saved === items.length) {
    return {label:"下载完成", message:`已保存 ${saved} 项，${state.publicMode === true ? "请保存到本机" : "可导出原件"}。`, hasJobs};
  }
  if (current.some(job => ["queued", "running"].includes(job.status))) {
    return {label:"下载中", message:`已自动加入下载队列 · 已保存 ${saved} / ${items.length} 项`, active:true, hasJobs};
  }
  const stopped = current.find(job => ["failed", "needs-user", "paused", "cancelled"].includes(job.status));
  if (stopped) {
    const labels = {failed:"下载失败", "needs-user":"源站限制访问", paused:"下载已暂停", cancelled:"下载已取消"};
    return {label:labels[stopped.status], message:stopped.error || "可在下载任务中查看详情、继续或重试。", warning:true, hasJobs};
  }
  const summary = result.downloadSummary || scan?.downloadSummary;
  if (summary?.error) return {label:"下载未开始", message:summary.error, warning:true, hasJobs};
  if (summary?.blocked) return {label:"部分素材无法下载", message:`${summary.blocked} 项素材受源站权限或播放格式限制。`, warning:true, hasJobs};
  if (summary?.added || summary?.duplicates) return {label:"已加入下载队列", message:"已提交下载请求，可在活动中查看任务状态。", hasJobs:true};
  return {label:items.length ? "下载未开始" : "未找到可下载素材",
    message:items.length ? "请检查下方素材，可手动加入下载队列。" : result.message || "请确认链接指向公开可访问的作品。", hasJobs};
}

function currentDownloadJobs(jobs) {
  const byAsset = new Map();
  for (const job of jobs || []) {
    const previous = byAsset.get(job.assetId);
    if (pendingJobs.has(job.status) || !pendingJobs.has(previous?.status)) byAsset.set(job.assetId, job);
  }
  return byAsset;
}

export function renderIntakeResult(result, state, view, {error = "", loading = false} = {}) {
  view = {...view, publicMode:state.publicMode === true, jobs:currentDownloadJobs(state.jobs)};
  const active = result && ["queued","running"].includes(result.status);
  const scopeLabel = intakeScopes.find(([id]) => id === result?.scope)?.[1] || "";
  const header = `<header class="page-heading"><div class="page-heading-copy"><h1>拾取结果</h1>${result ? `<p class="intake-result-source"><strong>${scopeLabel}</strong> · ${esc(result.url)}${result.previousScanId ? " · 含前序轮次的拾取结果" : ""}</p>` : ""}</div><div class="page-heading-actions"><button class="btn btn-secondary" data-action="scan-history">${icon("clock",15)}扫描历史</button><button class="btn btn-primary" data-action="new-scan">${icon("plus",15)}拾取链接</button></div></header>`;
  const errorHtml = error ? `<div class="intake-result-notice" role="alert">${esc(error)} <button class="btn btn-ghost" data-action="intake-refresh">重新读取</button></div>` : "";
  if (!result) return `${header}${errorHtml || `<div role="status" class="intake-result-notice">${loading ? "正在读取本次结果…" : "请选择一次拾取记录。"}</div>`}`;
  const groups = result.groups || [];
  const assets = new Map(state.assets.map(a => [a.id, a]));
  const reserved = reservedAssets(state);
  const all = resultItems(result, state);
  const selected = all.filter(a => view.selected.has(a.id));
  const download = directDownloadStatus(result, state, all, view.jobs);
  const warning = result.limited || download?.warning || !["queued","running","completed"].includes(result.status);
  const publicRestriction = view.publicMode && result.status === "needs-user";
  const label = publicRestriction ? "源站限制访问" : download?.label || statusLabels[result.status] || result.status;
  const message = publicRestriction ? `这是来源站点的限制，拾帧无需登录。${result.message || "请稍后重试，或更换公开可访问的链接。"}` : download?.message || result.message || "";
  const siteId = state.scans?.find(scan => scan.id === result.scanId)?.siteId;
  const top = `<div class="intake-result-status ${warning ? "has-warning" : ""}" role="status"><span>${icon(active || download?.active ? "clock" : warning ? "alertCircle" : "checkCircle",17)}<strong>${esc(label)}</strong>${result.limited ? " · 范围已达上限" : ""}</span><span>${esc(message)}</span>${result.status === "needs-user" && siteId && !view.publicMode ? `<button class="btn btn-secondary" data-action="auth-site" data-id="${esc(siteId)}">管理站点授权</button>` : ""}${download?.hasJobs ? `<button class="btn btn-secondary" data-action="navigate" data-page="queue">查看下载任务</button>` : ""}${active ? `<button class="btn btn-ghost" data-action="cancel-scan" data-id="${esc(result.scanId)}">取消解析</button>` : ""}${roundActions(result,view)}</div>`;
  const categories = [...new Set(groups.flatMap(g => g.categories || []))].filter(Boolean);
  const matching = groups.filter(g => (!view.category || g.categories?.includes(view.category))
    && (!view.query || [g.title, g.url, ...(g.categories || [])].join(" ").toLowerCase().includes(view.query.toLowerCase())));
  const groupPage = pageRows(matching, view.groupPage, 12);
  const group = groupPage.rows.find(g => g.key === view.groupKey) || groupPage.rows[0];
  const items = group ? resultItems(result,state,{group:group.key}) : [];
  const list = result.scope === "site" && groups.length ? `<section class="intake-show-index"><div class="intake-index-head"><h2>剧集目录 <small>${groups.length}</small></h2>${categories.length ? `<select class="select" id="intake-category" aria-label="剧集分类"><option value="">全部分类</option>${categories.map(c => `<option value="${esc(c)}" ${view.category === c ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>` : ""}${groups.length > 1 ? `<label><span class="sr-only">搜索剧名或分类</span><input class="input" id="intake-group-query" type="search" placeholder="搜索剧名或分类" value="${esc(view.query)}"></label>` : ""}</div>
    <div class="intake-show-options">${groupPage.rows.map(g => `<button type="button" class="intake-show-option" aria-pressed="${g.key === group?.key}" data-action="intake-group" data-value="${esc(g.key)}">${icon("film",14)}<span>${esc(g.title || "剧集")}</span><small>${g.category ? `${esc(g.category)} · ` : ""}${g.episodes?.length ? `${g.episodes.length} 集` : "目录待确认"}</small></button>`).join("")}</div>${!matching.length ? '<p class="intake-muted">没有符合筛选的剧集。</p>' : ""}${pager("groupPage",groupPage)}</section>` : "";
  const bulk = all.length ? `<div class="intake-result-bulk"><div><button type="button" class="btn btn-ghost" data-action="intake-select">全选本次</button>${selected.length ? `<button class="btn btn-ghost" data-action="intake-clear">清空选择</button>` : ""}<span>已选 ${selected.length} / ${all.length} 项</span></div>${batch(selected,view,reserved,' data-selected="1"')}</div>` : "";
  return `<section class="intake-result-page">${header}${errorHtml}${top}${list}${bulk}${group ? groupContent(group,result,view,assets,reserved,items) : !groups.length && !active ? '<div class="empty-state is-calm"><h3>本次暂无可用结果</h3><p>可从剧集详情或单集链接重新拾取。</p></div>' : ""}</section>`;
}

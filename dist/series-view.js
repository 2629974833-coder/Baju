import { icon, escapeHtml as esc } from './ui.js';
import { accessPending, catalogCountLabel, catalogIssueMessage, episodeStatus, renderEpisodeCatalog, selectCatalogEpisodes, seriesCoinDraft } from './catalog-view.js';

const active = scan => ['queued', 'running'].includes(scan?.status);
const siteScan = scan => scan?.mode === 'site' || scan?.intakeScope === 'site';
const scanLabels = {
  queued:'等待扫描', running:'正在逐集解析', completed:'本轮扫描结束',
  failed:'扫描失败', cancelled:'扫描已取消', 'needs-user':'需要登录或人工验证',
  'timed-out':'扫描超时', 'unsupported-drm':'媒体受 DRM 保护', 'unsupported-media':'暂不支持此播放格式',
};

function scanWarning(scan, noCatalog) {
  return ['failed', 'needs-user', 'timed-out', 'unsupported-drm','unsupported-media'].includes(scan.status)
    || (scan.status === 'completed' && (noCatalog || scan.error));
}

function scanProgress(scan, noCatalog = false) {
  if (!scan || scan.seriesNoticeDismissed) return '';
  const warning = scanWarning(scan, noCatalog);
  let message = scan.error || scan.message || '';
  if (!scan.error && noCatalog && scan.status === 'completed') {
    const issue = catalogIssueMessage(scan.catalog);
    message = issue
      ? `${issue}。`
      : '尚未识别到剧集目录，可尝试有明确选集列表的详情页或播放页。';
  }
  const legacyReadOnly = scan.status === 'needs-user' && message.startsWith('只读扫描已阻止写入或实时请求');
  if (legacyReadOnly) message = '上次扫描因只读限制中止，可重新读取目录。';
  const checking = Number.isInteger(scan.checkTotal) && scan.checkTotal > 0;
  let statusLabel = scanLabels[scan.status] || '扫描记录';
  if (checking && scan.status === 'running') statusLabel = '正在扫描并检查';
  if (checking && scan.status === 'completed') statusLabel = '扫描并检查结束';
  const progressLabel = checking
    ? `已检查 ${Number(scan.checkedCount) || 0} / ${scan.checkTotal} 集`
    : `已读取 ${Number(scan.pagesVisited) || 0} 页${Number(scan.pagesQueued) > 0 ? ` · 待读取 ${Number(scan.pagesQueued)} 页` : ''}`;
  const clearable = warning || scan.status === 'cancelled';
  const controls = clearable ? `<span class="series-notice-actions">${noCatalog ? `<button type="button" class="btn btn-ghost" data-action="series-notice-retry" data-id="${esc(scan.id)}">填写链接重试</button>` : ''}
    <button type="button" class="btn btn-ghost" data-action="series-notice-dismiss" data-id="${esc(scan.id)}">清除提示</button></span>` : '';
  return `<div class="series-scan-status${warning ? ' is-warning' : ''}" role="status">
    <div>${icon(active(scan) ? 'refresh' : warning ? 'alertCircle' : 'checkCircle', 15)}<strong>${legacyReadOnly ? '目录读取受限' : statusLabel}</strong><span>${progressLabel}</span>
    ${active(scan) ? `<button class="btn btn-ghost" data-action="cancel-scan" data-id="${esc(scan.id)}">取消扫描</button>` : ''}${controls}</div>
    ${message ? `<p>${esc(message)}</p>` : ''}${noCatalog ? `<small>${esc(scan.url || '')}</small>` : ''}</div>`;
}

function downloadReport(scan) {
  const result = scan?.downloadSummary;
  if (!result) return '';
  if (result.error) return `<p class="series-download-report is-warning" role="status">下载任务未提交：${esc(result.error)}</p>`;
  const parts = [`已加入 ${Number(result.added) || 0} 个下载任务`];
  if (result.duplicates > 0) parts.push(`${Number(result.duplicates)} 项已保存或已在队列`);
  if (Array.isArray(result.unavailable) && result.unavailable.length) parts.push(`${result.unavailable.length} 集暂不可下载`);
  return `<p class="series-download-report" role="status">${parts.join(' · ')}</p>`;
}

function coinReport(scan) {
  const summary = scan?.unlockSummary;
  if (!summary) return '';
  const reasons = Object.entries(summary.reasons || {}).filter(([,count]) => Number.isInteger(count) && count > 0);
  const reasonsText = reasons.map(([reason,count]) => `${reason}（${count} 集）`).join('、');
  const remaining = [];
  if (summary.skipped > 0) remaining.push(`跳过 ${Number(summary.skipped)} 集`);
  if (summary.unknown > 0) remaining.push(`交易待核实 ${Number(summary.unknown)} 集`);
  return `<div class="series-coin-report" role="status">
    <strong>${active(scan) ? '本次金币解锁' : '最近一次金币解锁'}</strong>
    <span>已解锁 ${Number(summary.unlocked) || 0} 集 · 已确认支出 ${Number(summary.spent) || 0} 金币${remaining.length ? ' · ' + remaining.join(' · ') : ''}</span>
    ${reasonsText ? `<p>${esc(reasonsText)}</p>` : ''}
    ${summary.stopped && summary.lastMessage ? `<p>${esc(summary.lastMessage)}</p>` : ''}
  </div>`;
}

function episodeReceipts(item, sourceIds, scans) {
  const receipts = new Map();
  const pages = new Map((item.catalog?.entries || []).map(entry => [entry.episodeKey, entry.pageUrl]));
  // Scans are newest first. A later scan/skip cannot erase a recorded charge.
  for (const scan of scans) {
    if (!scan.unlockResults || !sourceIds.has(scan.id)) continue;
    for (const entry of scan.catalog?.entries || []) {
      const key = entry.episodeKey;
      if (receipts.has(key) || !entry.pageUrl || pages.get(key) !== entry.pageUrl) continue;
      const receipt = scan.unlockResults[key];
      if (!receipt) continue;
      let label;
      if (receipt.status === 'purchased' && Number.isSafeInteger(receipt.spent) && receipt.spent >= 0) label = `已付 ${receipt.spent} 金币`;
      else if (receipt.status === 'pending') label = '付款确认中';
      else if (receipt.status === 'uncertain') label = '扣款待核实';
      else if (receipt.status === 'owned' && receipt.reconciled) label = '权益已确认，扣款待核实';
      if (label) receipts.set(key, {label, message:receipt.message});
    }
  }
  return receipts;
}

function ungroupedScans(state, series, submittedScanId) {
  const seen = new Set();
  return (state.scans || []).filter(scan => {
    const submittedAttempt = scan.id === submittedScanId && (scan.status !== 'completed' || !scan.catalog?.entries?.length);
    if (scan.mode !== 'series') return false;
    const key = JSON.stringify([scan.siteId, scan.url]);
    if (seen.has(key) && !active(scan)) return false;
    // Even a resolved or cleared notice supersedes an earlier attempt at this URL.
    seen.add(key);
    if (scan.seriesNoticeDismissed || (scan.seriesDismissed && !submittedAttempt) || series.some(item => item.scanId === scan.id || (
      item.siteId === scan.siteId && item.catalog?.seriesKey &&
      item.catalog.seriesKey === scan.catalog?.seriesKey &&
      item.catalog.adapterId === scan.catalog?.adapterId
    ))) return false;
    return true;
  }).slice(0, 5);
}

const usableThumb = url => typeof url === 'string' && url && !url.endsWith('favicon.svg');

function indexAssets(state) {
  return new Map((state.assets || []).map(asset => [asset.id, asset]));
}

export function episodeThumbnails(item, state, assets = indexAssets(state)) {
  const thumbs = {};
  for (const entry of item.catalog?.entries || []) {
    const ids = [entry.downloadAssetId, ...(entry.assetIds || [])].filter(Boolean);
    const asset = ids.map(id => assets.get(id)).find(asset => usableThumb(asset?.thumbnail));
    if (asset) thumbs[entry.episodeKey] = asset.thumbnail;
  }
  return thumbs;
}

export function seriesPoster(item, state, thumbs = episodeThumbnails(item, state)) {
  const title = item.title || item.catalog?.title || item.catalog?.seriesKey || '未命名剧集';
  const localPoster = `/api/series/${encodeURIComponent(item.id)}/poster`;
  const source = item.posterUrl === localPoster ? localPoster : Object.values(thumbs)[0];
  let hash = 0;
  for (const char of title) hash = (hash * 31 + char.codePointAt(0)) % 360;
  return `<span class="series-poster is-type" style="--poster-hue:${hash}" aria-hidden="true"><b>${esc([...title.replace(/[《》\s·]/g, '')][0] || '剧')}</b><small>${esc(title.slice(0, 12))}</small>${source ? `<img data-series-poster src="${esc(source)}" alt="" loading="lazy">` : ''}</span>`;
}

export function seriesStrip(item, limit = 60) {
  const entries = item.catalog?.entries || [];
  const cells = entries.slice(0, limit).map(entry => `<i data-media-status="${esc(episodeStatus(entry))}"></i>`).join('');
  return `<span class="series-strip" aria-hidden="true">${cells}${entries.length > limit ? `<em>+${entries.length - limit}</em>` : ''}</span>`;
}

const categoriesOf = item => [...new Set((Array.isArray(item.categories) ? item.categories : []).filter(value => typeof value === 'string' && value.trim()))];

export function filterSeriesItems(series, { query = '', siteId = '', category = '' } = {}) {
  const term = query.trim().toLocaleLowerCase();
  return series.filter(item => (!siteId || item.siteId === siteId) && (!category || categoriesOf(item).includes(category)) &&
    [item.title, item.sourceTitle, item.catalog?.title, item.catalog?.seriesKey].some(value => String(value || '').toLocaleLowerCase().includes(term)));
}

export const SERIES_PAGE_SIZE = 12;

export function seriesPageItems(series, filters = {}) {
  const filtered = filterSeriesItems(series, filters);
  const pages = Math.max(1, Math.ceil(filtered.length / SERIES_PAGE_SIZE));
  const page = Math.min(pages, Math.max(1, Math.floor(Number(filters.page) || 1)));
  return {items:filtered.slice((page - 1) * SERIES_PAGE_SIZE, page * SERIES_PAGE_SIZE), total:filtered.length, page, pages};
}

function artworkJobs(item, state) {
  const ids = new Set(item.artwork?.assetIds || []);
  return (state.jobs || []).filter(job => ids.has(job.assetId));
}

function artworkDownloading(item, state) {
  return (item.artwork?.saved || 0) < (item.artwork?.total || 0) && artworkJobs(item, state).some(active);
}

function artworkStatus(item, state) {
  const art = item.artwork;
  if (!art) return item.posterUrl ? '海报随本剧一起保存' : '暂无站点海报';
  if (!art.total) return '暂无站点海报';
  if (art.saved >= art.total) return art.total === 1 ? '海报已保存' : `海报已保存 ${art.saved} 张`;
  const jobs = artworkJobs(item, state);
  if (jobs.some(job => job.status === 'running')) return '海报正在下载';
  if (jobs.some(job => job.status === 'queued')) return '海报已入队';
  if (jobs.some(job => ['failed', 'needs-user'].includes(job.status))) return '海报下载失败，可重试下载本剧';
  return art.saved ? `海报已保存 ${art.saved} / ${art.total} 张` : '海报待下载';
}

function showActions(item, state, ownScan, submitting) {
  const busy = active(ownScan);
  const waitingForPoster = artworkDownloading(item, state);
  const canExport = !waitingForPoster && state.seriesArchiveAvailable === true && ((Number(item.catalog?.downloadedCount) || 0) > 0 || (Number(item.artwork?.saved) || 0) > 0);
  const exportLabel = waitingForPoster ? '等待海报保存…' : item.artwork?.saved > 0 ? '导出本剧（含海报）' : '导出本剧（ZIP）';
  const label = submitting ? '正在提交…' : busy ? ownScan.downloadAfterScan ? '正在扫描并下载…' : '正在扫描…' : '下载本剧';
  return `<div class="series-show-actions"><div class="series-show-buttons">
    <button type="button" class="btn btn-primary" data-action="series-download-all" data-id="${esc(item.id)}" ${submitting || busy || (!item.catalog?.entries?.length && !item.directoryPending) || state.mode !== 'real' ? 'disabled' : ''}>${icon(busy ? 'refresh' : 'download',14)}${label}</button>
    ${canExport ? `<a class="btn btn-secondary" href="/api/series/${encodeURIComponent(item.id)}/archive" download>${exportLabel}</a>` : `<button type="button" class="btn btn-secondary" disabled title="${waitingForPoster ? '海报保存后即可连同视频导出' : '先下载本剧的视频或海报'}">${exportLabel}</button>`}
    </div><p class="series-download-hint">扫描可访问单集并下载，含海报 <span>${esc(artworkStatus(item, state))}</span></p></div>`;
}

function seriesCard(item, state, { selected, expanded, checkedSeries, episodeFilters, openByDefault, assetsById, submittingCheck, coinDrafts, downloadSubmitting, flash = false }) {
  const catalog = item.catalog || {};
  const ready = selectCatalogEpisodes(catalog, 'ready').length;
  const pending = selectCatalogEpisodes(catalog, 'pending').length;
  const blocked = selectCatalogEpisodes(catalog, 'needs-user').length;
  const unverified = (catalog.entries || []).filter(entry => accessPending(entry, catalog)).length;
  const scan = (state.scans || []).find(scan => scan.id === item.scanId);
  const coinDraft = seriesCoinDraft(state, item, coinDrafts);
  const sourceIds = new Set([...(item.sourceScanIds || []), item.scanId]);
  const ownScan = (state.scans || []).find(s => active(s) && (sourceIds.has(s.id) || s.seriesId === item.id)) || scan;
  const paidScan = scan?.unlockSummary ? scan : (state.scans || []).find(s => s.unlockSummary && sourceIds.has(s.id));
  const receipts = episodeReceipts(item, sourceIds, state.scans || []);
  const scanning = submittingCheck || (state.scans || []).some(active);
  const site = (state.sites || []).find(site => site.id === item.siteId);
  const total = catalogCountLabel(catalog);
  const title = item.title || catalog.title || catalog.seriesKey || '未命名剧集';
  const catalogCode = catalog.seriesKey && !/^generic-/i.test(catalog.seriesKey) ? catalog.seriesKey : '';
  const count = catalog.entries?.length || 0;
  const counts = item.directoryPending ? '选集目录待读取' : !catalog.truncated && count === catalog.publishedCount ? total : `目录内 ${count} 集 · ${total}`;
  const dismissed = Number(catalog.dismissedEpisodeCount) || 0;
  const permissionLabel = unverified && unverified === blocked ? '待查权限' : unverified ? '权限相关' : '需权限';
  const metrics = [['可下载',ready],['已保存',Number(catalog.downloadedCount)||0],['待解析',pending],[permissionLabel,blocked]];
  const permissionNote = [
    blocked > unverified ? `站点要求处理权限或验证：${blocked - unverified} 集` : '',
    unverified ? `账号权限待核实：${unverified} 集` : '',
  ].filter(Boolean).join('；');
  const isChecked = checkedSeries.has(item.id);
  const seriesActions = `<div class="series-primary-actions"><button class="btn btn-primary" data-action="series-download-ready" data-id="${esc(item.id)}" ${!ready ? 'disabled' : ''}>下载本剧可用集（${ready}）</button></div>
    ${blocked ? `<div class="series-access-actions"><div><button class="btn btn-ghost" data-action="auth-site" data-id="${esc(item.siteId)}">管理站点权限</button></div><p>${esc(permissionNote)}。</p></div>` : ''}`;
  const thumbs = episodeThumbnails(item, state, assetsById);
  return `<article class="series-card${flash ? ' is-flash' : ''}" data-series-id="${esc(item.id)}">
    <div class="series-card-heading">${seriesPoster(item, state, thumbs)}<label class="series-card-pick" title="选择整部剧"><input id="series-pick-${esc(item.id)}" type="checkbox" data-series-check="${esc(item.id)}" aria-label="选择整部剧：${esc(title)}" ${isChecked ? 'checked' : ''}></label>
    <div class="series-card-name"><span class="series-site" ${catalogCode ? `title="剧集编号 ${esc(catalogCode)}"` : ''}>${icon('globe', 12)}${esc(site?.name || '未知来源')}${catalogCode && catalogCode !== title ? `<span class="series-key">${esc(catalog.seriesKey)}</span>` : ''}</span><h2>${esc(title)}</h2>${categoriesOf(item).length ? `<div class="series-category-tags">${categoriesOf(item).map(category => `<span>${esc(category)}</span>`).join('')}</div>` : ''}<p>${counts}${dismissed ? ` · 已移除 ${dismissed} 集` : ''}</p></div>
      <div class="series-card-tools"><button type="button" class="btn btn-ghost" data-action="series-rename" data-id="${esc(item.id)}">改名</button><button class="btn btn-ghost" data-action="series-rescan" data-id="${esc(item.id)}" ${scanning ? 'disabled' : ''}>${icon('refresh',14)}重新读取目录</button></div>
    <div class="series-overview">${seriesStrip(item, 120)}<div class="series-metrics">${metrics.map(([label,value]) => `<div><span>${label}</span><strong>${value}<small> 集</small></strong></div>`).join('')}</div></div></div>
    ${showActions(item, state, ownScan, downloadSubmitting.has(item.id))}
    ${siteScan(scan) || (scan?.status === 'completed' && !scan.error && !scan.checkTotal) ? '' : scanProgress(scan)}
    ${downloadReport(scan)}
    ${coinReport(paidScan)}
    ${renderEpisodeCatalog({...item, status:scan?.status || item.status}, selected[item.id] || [], {seriesId:item.id, archiveAvailable:state.seriesArchiveAvailable === true, archivePending:artworkDownloading(item, state), open:expanded[item.id] ?? openByDefault, scanning, seriesActions, filter:episodeFilters[item.id] || {}, thumbnails:thumbs, coinDraft, receipts})}
  </article>`;
}

function seriesPager(page) {
  if (page.pages < 2) return '';
  return `<nav class="series-pager" aria-label="剧集分页"><button type="button" class="btn btn-secondary" data-action="series-page" data-value="${page.page - 1}" ${page.page === 1 ? 'disabled' : ''}>上一页</button><span aria-live="polite">${page.page} / ${page.pages}</span><button type="button" class="btn btn-secondary" data-action="series-page" data-value="${page.page + 1}" ${page.page === page.pages ? 'disabled' : ''}>下一页</button></nav>`;
}

function seriesToolbar(state, filters, page, checked) {
  const n = page.items.filter(item => checked.has(item.id)).length;
  const allChecked = filterSeriesItems(state.series || [], filters).filter(item => checked.has(item.id)).length;
  const sites = new Set((state.series || []).map(item => item.siteId));
  const categories = [...new Set((state.series || []).filter(item => !filters.siteId || item.siteId === filters.siteId).flatMap(categoriesOf))].sort((a,b) => a.localeCompare(b,'zh-CN'));
  if (filters.category && !categories.includes(filters.category)) categories.push(filters.category);
  return `<section class="series-library-tools" aria-label="剧集筛选与批量管理">
    <div class="series-filter-row"><div class="filter-search">${icon('search',14)}<input type="search" id="series-library-query" data-series-filter="query" aria-label="搜索剧名或剧集编号" placeholder="搜索剧名或剧集编号" value="${esc(filters.query || '')}"></div>
      <select class="filter-control" id="series-library-site" data-series-filter="siteId" aria-label="筛选剧集来源"><option value="">全部来源</option>${(state.sites || []).filter(site => sites.has(site.id) || site.id === filters.siteId).map(site => `<option value="${esc(site.id)}" ${site.id === filters.siteId ? 'selected' : ''}>${esc(site.name)}</option>`).join('')}</select>
      <select class="filter-control" id="series-library-category" data-series-filter="category" aria-label="筛选剧集分类"><option value="">全部分类</option>${categories.map(category => `<option value="${esc(category)}" ${category === filters.category ? 'selected' : ''}>${esc(category)}</option>`).join('')}</select>
      ${filters.query || filters.siteId || filters.category ? '<button class="btn btn-ghost" data-action="series-clear-filters">清除剧集筛选</button>' : ''}
    </div>
    <div class="series-selection-row"><label><input type="checkbox" id="series-pick-visible" data-action="series-check-visible" ${page.items.length && n === page.items.length ? 'checked' : ''} ${page.items.length ? '' : 'disabled'}>全选本页</label>
      <span aria-live="polite">共 ${page.total} 部 · 本页 ${page.items.length} 部${allChecked ? ` · 已选 ${allChecked} 部` : ''}</span>
      ${allChecked ? '<button class="btn btn-danger" data-action="series-dismiss-checked">移除所选整剧</button>' : ''}
    </div>${seriesPager(page)}</section>`;
}

function sourceGroups(items, state, renderCard) {
  const groups = new Map();
  for (const item of items) {
    if (!groups.has(item.siteId)) groups.set(item.siteId, []);
    groups.get(item.siteId).push(item);
  }
  return [...groups].map(([siteId, shows]) => {
    const site = (state.sites || []).find(site => site.id === siteId);
    return `<section class="series-source-group" data-series-source="${esc(siteId)}"><header class="series-source-heading"><h2>${icon('globe',15)}${esc(site?.name || '未知来源')}</h2><span>本页 ${shows.length} 部</span></header><div class="series-library">${shows.map(renderCard).join('')}</div></section>`;
  }).join('');
}

export function renderSiteScanProgress(state, siteId = '') {
  const newest = new Map();
  for (const scan of state.scans || []) {
    if (!siteScan(scan) || (siteId && scan.siteId !== siteId)) continue;
    if (!newest.has(scan.siteId)) newest.set(scan.siteId, scan);
  }
  const scans = [...newest.values()].filter(scan => !scan.seriesNoticeDismissed);
  if (!scans.length) return '';
  return `<section class="series-site-progress" aria-label="整站拾取进度">${scans.map(scan => {
    const site = (state.sites || []).find(site => site.id === scan.siteId);
    const summary = scan.siteSummary || {};
    const shows = Number(summary.shows) || 0;
    const episodes = Number(summary.episodes) || 0;
    return `<article data-site-scan="${esc(scan.id)}"><div class="series-site-progress-heading"><strong>整站拾取 · ${esc(site?.name || '来源站点')}</strong>${!active(scan) ? `<button type="button" class="icon-button series-notice-close" data-action="series-notice-close" data-id="${esc(scan.id)}" aria-label="关闭${esc(site?.name || '来源站点')}的拾取通知" title="关闭通知，保留扫描历史">${icon('x',16)}</button>` : ''}</div>
      ${scanProgress(scan)}<div class="series-site-progress-footer"><span>已列出 ${shows} 部 · ${episodes} 集${Number.isInteger(summary.directories) && summary.directories < shows ? ` · 已读目录 ${summary.directories} / ${shows}` : ''}</span>
      ${scan.canContinue && !active(scan) ? `<button type="button" class="btn btn-secondary" data-action="series-continue-site" data-id="${esc(scan.id)}">继续读取目录</button>` : ''}
      ${scan.status === 'needs-user' ? `<button type="button" class="btn btn-secondary" data-action="auth-site" data-id="${esc(scan.siteId)}">管理站点权限</button>` : ''}
      <button type="button" class="btn btn-ghost" data-action="navigate" data-page="home">回总览拾取其他链接</button></div></article>`;
  }).join('')}</section>`;
}

export function renderSeriesLibrary(state, { draft = {}, selected = {}, expanded = {}, filters = {}, checkedSeries = [], episodeFilters = {}, scanOptionsOpen = false, submittedScanId = null, addOpen = false, flashScanId = '', submittingCheck = false, coinDrafts = {}, downloadSubmitting = new Set() } = {}) {
  const series = state.series || [];
  const page = seriesPageItems(series, filters);
  const visible = page.items;
  const assetsById = visible.length ? indexAssets(state) : null;
  const checked = new Set(checkedSeries);
  const real = state.mode === 'real';
  const scanning = submittingCheck || (state.scans || []).some(active);
  const recent = ungroupedScans(state, series, submittedScanId);
  const notices = recent.filter(scan => scanWarning(scan, true) || scan.status === 'cancelled');
  const savedTotal = series.reduce((sum, item) => sum + (Number(item.catalog?.downloadedCount) || 0), 0);
  const addExpanded = addOpen || !series.length;
  return `<header class="page-heading"><div class="page-heading-copy"><div class="eyebrow">整剧管理</div><h1>剧集</h1><p>${series.length ? `${series.length} 部 · 已保存 ${savedTotal} 集` : '粘贴详情页或单集链接，读取站点提供的选集目录。'}</p></div><div class="page-heading-actions"><button class="btn btn-secondary" data-action="navigate" data-page="queue">${icon('pulse',15)}<span class="secondary-label">查看活动</span></button></div></header>
    <details class="series-add ${series.length ? '' : 'is-first'}" data-series-add ${addExpanded ? 'open' : ''}><summary class="series-add-summary"><span class="series-add-plus">${icon('plus',16)}</span><span class="series-add-copy"><strong>添加剧集</strong><small>粘贴详情页或单集链接，读取选集目录</small></span><span class="series-add-chevron">${icon('chevronDown',15)}</span></summary>
      <form id="series-scan-form"><div class="series-url-row"><label class="sr-only" for="series-url">剧集详情页或播放页网址</label><input class="input" id="series-url" name="url" type="text" value="${esc(draft.url || '')}" placeholder="粘贴详情页或任意一集链接…" required autocomplete="off" spellcheck="false" ${real ? '' : 'disabled'}><button class="btn btn-primary" type="submit" ${!real || scanning ? 'disabled' : ''}>${icon('search',16)}扫描整部剧</button></div>
      <div class="series-add-options"><div class="series-name-field"><label for="series-custom-title">剧名</label><input class="input" id="series-custom-title" name="customTitle" type="text" maxlength="100" value="${esc(draft.customTitle || '')}" placeholder="选填，留空使用站点剧名" ${real ? '' : 'disabled'}></div>
      <details class="series-scan-settings" data-series-scan-settings ${scanOptionsOpen ? 'open' : ''}><summary>更多设置</summary><div class="series-scan-options"><div><label for="series-site">来源站点</label><select class="select" id="series-site" name="siteId"><option value="">按网址自动识别</option>${(state.sites || []).map(site => `<option value="${esc(site.id)}" ${draft.siteId === site.id ? 'selected' : ''}>${esc(site.name)}</option>`).join('')}</select></div><div><label for="series-limit">本轮最多读取</label><input class="input" id="series-limit" name="limit" type="number" min="1" max="1000" value="${Number(draft.limit) || 100}" required><span>页</span></div><p>${real ? scanning ? '已有扫描正在进行，结束后可以提交下一部。' : '达到页数上限后，可从目录继续扫描。' : '整剧扫描需要启动本地真实服务。'}</p></div></details></div><p id="series-scan-error" class="form-error" role="alert"></p></form>
    </details>
    ${renderSiteScanProgress(state, filters.siteId)}
    ${recent.length ? `<section class="series-recent" aria-label="整剧扫描进度">${notices.length > 1 ? `<div class="series-notice-bulk"><button type="button" class="btn btn-ghost" data-action="series-notices-dismiss" data-ids="${esc(JSON.stringify(notices.map(scan => scan.id)))}">清除当前提示（${notices.length}）</button></div>` : ''}${recent.map(scan => scanProgress(scan, true)).join('')}</section>` : ''}
    ${series.length ? seriesToolbar(state, filters, page, checked) : ''}
    ${visible.length ? `${sourceGroups(visible, state, item => seriesCard(item, state, {selected,expanded,checkedSeries:checked,episodeFilters,openByDefault:page.total === 1,assetsById,submittingCheck,coinDrafts,downloadSubmitting,flash:Boolean(flashScanId) && item.scanId === flashScanId}))}${seriesPager(page)}` : `<div class="empty-state series-empty"><div class="empty-filter-icon">${icon(series.length ? 'search' : 'folder',25)}</div><h3>${series.length ? '没有符合筛选条件的剧集' : state.dismissedSeriesCount ? '目录中的整剧已全部移除' : '还没有归纳的剧集'}</h3><p>${series.length ? '调整剧名、来源或分类筛选，查看其他剧集。' : state.dismissedSeriesCount ? '在上方「添加剧集」粘贴这部剧的链接，重新扫描即可将它加入剧集库。' : '在上方输入剧集链接，扫描后即可在这里选集和下载。'}</p></div>`}`;
}

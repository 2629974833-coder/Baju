import { escapeHtml as esc } from './ui.js';

const reasonLabels = {
  'missing-entries': '目录仍有缺集', 'unresolved-media': '部分页面尚未解析到正片',
  'blocked-entries': '部分集需要站点权限', truncated: '本次扫描已达到范围上限',
  'conflicting-counts': '页面显示的总集数不一致',
  'catalog-conflict': '目录编号或归属存在冲突，请检查来源页面',
  'structured-limit': '页面目录数据超过读取上限，结果可能不完整',
  'structured-invalid': '部分页面目录数据无法读取',
  'structured-conflict': '页面目录数据相互冲突，请检查来源页面',
  'unsupported-media': '部分集的播放格式暂不支持下载',
  'scan-failed': '部分集读取失败，可选中后重试',
  'ambiguous-media': '部分集的主媒体仍待确认',
  'catalog-limit': '目录枚举已达到上限',
  'catalog-stalled': '目录未继续展开，已保留当前观察结果',
  'route-changed': '扫描中页面发生跳转，目录可能不完整',
  'read-only-blocked': '部分请求被只读扫描阻止，可能需在授权窗口操作',
  'out-of-range-entries': '发现超出页面总集数的编号，需核对目录',
  'ordinal-limit': '异常编号已达到处理上限，目录可能不完整',
};

const mediaLabels = {
  ready: '主媒体已确认，可下载',
  downloaded: '已保存',
  'needs-user': '需要站点权限',
  unresolved: '待扫描页面',
  ambiguous: '多个正片候选，主媒体待确认',
  'unsupported-media': '暂不支持此播放格式',
  'unsupported-drm': '受 DRM 保护',
  failed: '扫描失败，可重试',
};

export function catalogIssueMessage(catalog) {
  const directoryIssues = ['catalog-conflict', 'structured-conflict', 'structured-invalid', 'structured-limit'];
  return directoryIssues.filter(reason => catalog?.reasons?.includes(reason))
    .map(reason => reasonLabels[reason]).join('；');
}

function entryMediaStatus(entry) {
  if (Object.hasOwn(mediaLabels, entry.mediaStatus)) return entry.mediaStatus;
  if (entry.downloaded) return 'downloaded';
  if (entry.access === 'needs-user') return 'needs-user';
  if (entry.downloadAssetId || entry.assetIds?.length === 1) return 'ready';
  if (entry.assetIds?.length > 1) return 'ambiguous';
  return 'unresolved';
}

export function episodeStatus(entry) {
  return entryMediaStatus(entry);
}

export function accessPending(entry, catalog) {
  return entry.access === 'needs-user' && entry.scanStatus !== 'needs-user' &&
    (entry.accessEvidence === 'public-directory' || catalog?.adapterId === 'chunlou');
}

function needsAccess(entry) {
  return entry.access === 'needs-user' || entryMediaStatus(entry) === 'needs-user';
}

export function discoveryCatalog(scan) {
  const catalog = scan?.catalog;
  if (scan?.discoveryCatalogDismissed || !catalog?.entries?.length) return null;
  const dismissed = new Set(scan.discoveryDismissedEpisodeKeys || []);
  if (!dismissed.size) return catalog;
  const entries = catalog.entries.filter(entry => !dismissed.has(entry.episodeKey));
  if (!entries.length) return null;
  const visible = {
    ...catalog, entries,
    enumeratedCount: entries.length,
    resolvedCount: entries.filter(entry => ['ready','downloaded'].includes(entryMediaStatus(entry))).length,
    downloadedCount: entries.filter(entry => entry.downloaded || entryMediaStatus(entry) === 'downloaded').length,
    blockedCount: entries.filter(needsAccess).length,
    ambiguousCount: entries.filter(entry => entryMediaStatus(entry) === 'ambiguous').length,
  };
  const stillApplies = {
    'unresolved-media': entries.some(entry => ['unresolved', 'ambiguous'].includes(entryMediaStatus(entry))),
    'unsupported-media': entries.some(entry => ['unsupported-media','unsupported-drm'].includes(entryMediaStatus(entry))),
    'scan-failed': entries.some(entry => entryMediaStatus(entry) === 'failed'),
    'blocked-entries': visible.blockedCount > 0,
    'ambiguous-media': visible.ambiguousCount > 0,
  };
  visible.reasons = (catalog.reasons || []).filter(reason => stillApplies[reason] !== false);
  return visible;
}

const statusFilters = {
  all: '全部状态', ready: '可下载', downloaded: '已保存',
  unresolved: '待解析', ambiguous: '待确认主媒体', 'needs-user': '需权限',
  'unsupported-media': '播放格式不支持', 'unsupported-drm': 'DRM 保护', failed: '扫描失败',
};

export function filterEpisodeEntries(catalog, { query = '', status = 'all' } = {}) {
  const term = query.trim().toLocaleLowerCase();
  const ordinal = /^(?:第\s*)?(\d+)(?:\s*集)?$/.exec(term);
  return (catalog?.entries || []).filter(entry => {
    if (status === 'needs-user' && !needsAccess(entry)) return false;
    if (status !== 'all' && status !== 'needs-user' && entryMediaStatus(entry) !== status) return false;
    if (!term) return true;
    if (ordinal) return Number(entry.ordinal) === Number(ordinal[1]);
    return String(entry.title || '').toLocaleLowerCase().includes(term);
  });
}

export function episodeSelectionScope(catalog, selected, filter) {
  const entries = filterEpisodeEntries(catalog, filter);
  const checked = new Set(selected);
  const checkedEntries = entries.filter(entry => checked.has(entry.episodeKey));
  return {entries, checkedEntries, targets:checkedEntries.length ? checkedEntries : entries};
}

export function selectCatalogEpisodes(catalog, mode, selected = []) {
  const entries = catalog?.entries || [];
  if (mode === 'all') {
    const checked = new Set(selected);
    return entries.every(entry => checked.has(entry.episodeKey)) ? [] : entries.map(entry => entry.episodeKey);
  }
  return entries.filter(entry => {
    const status = entryMediaStatus(entry);
    if (mode === 'needs-user') return needsAccess(entry);
    return mode === 'ready' ? status === 'ready' : ['unresolved', 'ambiguous'].includes(status);
  }).map(entry => entry.episodeKey);
}

export function catalogScanMessage(result, requestedCount) {
  const skipped = result.skippedEpisodeKeys?.length || 0;
  const message = `已提交 ${Math.max(0, requestedCount - skipped)} 集的页面扫描。`;
  return message + (skipped ? `跳过 ${skipped} 集需要站点权限，请先在授权窗口解锁。` : '');
}

export function catalogDownloadMessage(result) {
  const unavailable = result.unavailable || [];
  const parts = [`已加入 ${Number(result.added) || 0} 集`];
  if (unavailable.length) {
    const labels = { 'needs-user': '需要站点权限', unresolved: '尚未解析正片', ambiguous: '主媒体待确认', 'not-found': '目录中未找到' };
    const details = new Map((result.unavailableDetails || []).map(item => [item.episodeKey, item.reason]));
    const counts = new Map();
    for (const key of unavailable) {
      const label = labels[details.get(key)] || mediaLabels[details.get(key)] || '尚未解析或需要权限';
      counts.set(label, (counts.get(label) || 0) + 1);
    }
    if (details.size) {
      const reasons = [...counts].map(([label, count]) => `${label} ${count} 集`).join('、');
      parts.push(`${unavailable.length} 集暂不可下载（${reasons}）`);
    } else {
      parts.push(`${unavailable.length} 集尚未解析或需要权限`);
    }
  }
  parts.push(`${Number(result.duplicates) || 0} 集已下载或已在队列`);
  return parts.join('；') + '。';
}

export function catalogCountLabel(catalog) {
  if (!catalog.truncated && Number.isInteger(catalog.publishedCount)) {
    return `站点已发布 ${catalog.publishedCount} 集`;
  }
  if (catalog.expectedCount != null) return `页面标示 ${Number(catalog.expectedCount)} 集`;
  return catalog.ongoing ? '连载中' : '总数未知';
}

function catalogCoverage(catalog) {
  if (catalog.expectedCount == null) {
    const gaps = catalog.observedGapOrdinals || [];
    const observed = gaps.length ? `已观察编号之间的间隙：第 ${gaps.join('、')} 集。` : '';
    if (catalog.ongoing && Number.isInteger(catalog.publishedCount) && !catalog.truncated) {
      return `${observed}连载中，站点已发布 ${catalog.publishedCount} 集。`;
    }
    return `${observed}总数未知，无法判断全剧是否齐全。`;
  }
  const missing = catalog.missingEpisodeKeys || [];
  const count = Number(catalog.missingCount) || missing.length;
  if (!count) return '';
  const ordinals = missing.map(key => String(key).split(':').at(-1));
  return `目录缺集 ${count} 集${ordinals.length ? `：第 ${ordinals.join('、')} 集` : ''}。`;
}

function episodeFilters(id, { query = '', status = 'all' }, discovery) {
  const suffix = esc(id);
  const attribute = discovery ? 'data-catalog-filter' : 'data-episode-filter';
  const action = discovery ? 'catalog-clear-filters' : 'series-clear-episode-filters';
  const prefix = discovery ? 'catalog' : 'episode';
  return `<div class="series-filter-row episode-filter-row">
    <input type="search" class="input" id="${prefix}-query-${suffix}" ${attribute}="query" data-id="${suffix}" aria-label="搜索集号或单集标题" placeholder="搜索集号或单集标题" value="${esc(query)}">
    <select class="select" id="${prefix}-status-${suffix}" ${attribute}="status" data-id="${suffix}" aria-label="单集状态">${Object.entries(statusFilters).map(([value,label]) => `<option value="${value}" ${value === status ? 'selected' : ''}>${label}</option>`).join('')}</select>
    ${query || status !== 'all' ? `<button type="button" class="btn btn-ghost" data-action="${action}" data-id="${suffix}">清除单集筛选</button>` : ''}
  </div>`;
}

export function discoveryScanIdentity(scan) {
  const date = new Date(scan.createdAt || scan.finishedAt);
  return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', {hour12:false}) : `扫描 ${scan.id}`;
}

function discoveryCard(scan, catalog, details, siteName) {
  const title = scan.customSeriesTitle || scan.catalog.title || scan.catalog.seriesKey || '未命名剧集';
  const removed = scan.catalog.entries.length - catalog.entries.length;
  const info = [siteName, discoveryScanIdentity(scan), removed ? `已移除 ${removed} 集` : ''].filter(Boolean).join(' · ');
  return `<article class="discovery-scan-card" aria-label="${esc(title)}的扫描目录">
    <header><div><h3>${esc(title)}</h3><small>${esc(info)}</small></div>
      <button type="button" class="btn btn-danger" data-action="catalog-dismiss-all" data-id="${esc(scan.id)}">移除整个目录</button>
    </header>${details}</article>`;
}

export function seriesCoinDraft(state, item, drafts = {}) {
  if (state.mode !== 'real' || !state.coinUnlockSiteIds?.includes(item.siteId)) return null;
  const draft = drafts[item.id];
  return {siteId:item.siteId, enabled:draft?.enabled === true, budget:String(draft?.budget ?? '')};
}

const budgetError = '预算须为正整数，或留空使用账号可用余额。';

function coinBudget(draft) {
  const value = draft.budget.trim();
  if (!value) return null;
  const amount = Number(value);
  return /^[0-9]+$/.test(value) && Number.isSafeInteger(amount) && amount > 0 ? amount : NaN;
}

export function seriesCheckBody(state, item, selected, filter, drafts) {
  const {targets} = episodeSelectionScope(item.catalog, selected, filter);
  if (!targets.length) throw new Error('当前没有可扫描的单集。');
  const body = {episodeKeys:targets.map(entry => entry.episodeKey), scanAndCheck:true};
  const draft = seriesCoinDraft(state, item, drafts);
  if (draft?.enabled) {
    const limit = coinBudget(draft);
    if (Number.isNaN(limit)) throw new Error(budgetError);
    body.autoUnlock = limit === null ? {} : {maxCoins:limit};
  }
  return body;
}

function coinControls(id, draft, limit, running) {
  if (!draft) return '';
  const key = esc(id);
  const invalid = Number.isNaN(limit);
  const disabled = running ? ' disabled' : '';
  const budgetHint = limit === null
    ? '将按站点报价逐集购买，费用以当前账号可用余额为限。'
    : '每集下单前，按站点报价核对账号余额和剩余预算。';
  const priceChangeHint = draft.siteId === 'lustflix' ? '若站点临时改价，以站点结算为准。' : '';
  return `<div class="series-coin-controls${draft.enabled ? ' is-enabled' : ''}">
    <label class="series-coin-toggle"><input id="coin-unlock-${key}" type="checkbox" data-coin-unlock="${key}"${draft.enabled ? ' checked' : ''}${disabled}><span>本剧自动用金币解锁</span></label>
    ${draft.enabled ? `<label class="series-coin-budget" for="coin-budget-${key}">本次预算 <input class="input" id="coin-budget-${key}" type="text" inputmode="numeric" autocomplete="off" data-coin-budget="${key}" value="${esc(draft.budget)}" placeholder="可用余额" aria-invalid="${invalid}" aria-describedby="coin-hint-${key}"${disabled}> 金币</label>
      <p id="coin-hint-${key}" class="${invalid ? 'form-error' : ''}">${invalid ? budgetError : '仅购买本次范围内未解锁的付费集。' + budgetHint + priceChangeHint} 本浏览器记住本剧设置。</p>` : ''}
  </div>`;
}

export function renderEpisodeCatalog(scan, selected = [], { archiveAvailable = false, archivePending = false, seriesId = null, discovery = false, open = !discovery, scanning = false, filter = {}, seriesActions = '', siteName = '', thumbnails = {}, coinDraft = null, receipts } = {}) {
  const catalog = discovery ? discoveryCatalog(scan) : scan.catalog;
  if (seriesId && !catalog?.entries?.length && catalog?.dismissedEpisodeCount) {
    return '<p class="series-empty-episodes">目录中的单集已全部移除。</p>';
  }
  if (!catalog?.entries?.length) return '';
  const running = scanning || ['queued', 'running'].includes(scan.status);
  const action = seriesId ? 'series' : 'catalog';
  const id = seriesId || scan.id;
  const total = catalogCountLabel(catalog);
  const reasons = (catalog.reasons || [])
    .filter(reason => catalog.expectedCount != null || reason !== 'missing-entries')
    .map(reason => reason === 'blocked-entries' && catalog.entries.some(entry => accessPending(entry, catalog))
      ? '部分集需要核对账号权限' : reasonLabels[reason]).filter(Boolean);
  const coverage = catalogCoverage(catalog);
  const outOfRange = catalog.outOfRangeOrdinals || [];
  const archiveCount = Number(scan.catalog.downloadedCount) || 0;
  const archive = archiveAvailable === true && archiveCount > 0;
  const sources = Array.isArray(catalog.discoverySources) ? catalog.discoverySources : [];
  const sourceLabels = [['dom', '页面选集'], ['json-ld', '结构化数据'], ['site-directory', '站点剧集目录']]
    .filter(([source]) => sources.includes(source)).map(([, label]) => label);
  const managed = Boolean(seriesId || discovery);
  const {entries, checkedEntries, targets} = episodeSelectionScope(catalog, selected, managed ? filter : undefined);
  const checked = new Set(selected);
  let checkLabel = '全剧';
  if (checkedEntries.length) checkLabel = '所选集';
  else if (filter.query?.trim() || (filter.status && filter.status !== 'all')) checkLabel = '当前结果';
  const budget = coinDraft?.enabled ? coinBudget(coinDraft) : null;
  const checkAction = coinDraft?.enabled ? '扫描并自动解锁' : '扫描并检查';
  const checkButton = seriesId ? `<button type="button" class="btn btn-primary" data-action="series-scan-check" data-id="${esc(id)}" ${running || !targets.length || Number.isNaN(budget) ? 'disabled' : ''}>${checkAction}${checkLabel}（${targets.length} 集）</button>` : '';
  const noneSelected = managed && !checkedEntries.length;
  const scanDisabled = running || (managed && checkedEntries.every(needsAccess));
  const downloadDisabled = managed && !checkedEntries.some(entry => entryMediaStatus(entry) === 'ready' && !needsAccess(entry));
  const scopeLabel = !managed ? '全选 / 清空' : entries.length && checkedEntries.length === entries.length ? '取消全选' : '全选当前结果';
  const selectButton = (mode, label, enabled) => `<button type="button" class="btn btn-ghost" data-action="${action}-select${mode}" data-id="${esc(id)}" ${enabled ? '' : 'disabled'}>${label}</button>`;
  const archiveLabel = discovery ? `导出本次扫描已保存的 ${archiveCount} 集` : seriesId ? '导出全部已保存集（ZIP）' : '导出已保存剧集';
  const archiveLink = !archive ? '' : archivePending ? '<button type="button" class="btn btn-secondary" disabled>等待海报保存…</button>' : `<a class="btn btn-secondary" href="/api/${seriesId ? 'series' : 'scans'}/${encodeURIComponent(id)}/archive" download>${archiveLabel}</a>`;
  const singleEpisode = checkedEntries.length === 1 ? checkedEntries[0] : null;
  const receiptNote = singleEpisode && receipts?.get(singleEpisode.episodeKey)?.message;
  const singleExport = archive && seriesId && singleEpisode && (singleEpisode.downloaded || entryMediaStatus(singleEpisode) === 'downloaded')
    ? `<a class="btn btn-secondary" href="/api/series/${encodeURIComponent(seriesId)}/episode-file?episodeKey=${encodeURIComponent(singleEpisode.episodeKey)}" download>导出第 ${Number(singleEpisode.ordinal)} 集原件</a>` : '';
  const detailsAttribute = seriesId ? ` data-series-catalog="${esc(seriesId)}"` : discovery ? ` data-discovery-catalog="${esc(id)}"` : '';
  const summary = managed ? `${discovery ? '剧集目录' : '单集目录'} <span>${catalog.entries.length} 集</span>` : `剧集目录 · 已枚举 ${Number(catalog.enumeratedCount) || 0} 集 · ${total}`;
  const dismissHint = discovery && noneSelected && entries.length ? ' · 请先勾选要移除的单集' : '';
  const dismissButton = managed ? `<button type="button" class="btn btn-danger" data-action="${discovery ? 'catalog' : 'series'}-dismiss-episodes" data-id="${esc(id)}" ${noneSelected && !discovery ? 'disabled' : ''}>${discovery ? '移除所选集' : '移除所选单集'}</button>` : '';
  const details = `<details class="episode-catalog"${detailsAttribute}${open ? ' open' : ''}><summary>${summary}</summary>
    <div class="episode-catalog-body">${seriesActions}${!seriesId ? `<p class="episode-counts">已解析 ${Number(catalog.resolvedCount) || 0} 集 · 已保存 ${Number(catalog.downloadedCount) || 0} 集 · 需权限 ${Number(catalog.blockedCount) || 0} 集 · 主媒体待确认 ${Number(catalog.ambiguousCount) || 0} 集</p>` : ''}
    ${sourceLabels.length ? `<p class="episode-source">目录依据：${sourceLabels.join('、')}</p>` : ''}
    ${coverage ? `<p class="episode-coverage">${esc(coverage)}</p>` : ''}
    ${outOfRange.length ? `<p class="episode-coverage">页面总集数范围外的已观察编号：第 ${esc(outOfRange.join('、'))} 集。</p>` : ''}
    ${reasons.length ? `<p class="episode-reasons">${reasons.map(esc).join('；')}。</p>` : ''}
    ${managed ? episodeFilters(id, filter, discovery) : ''}
    ${seriesId ? coinControls(id, coinDraft, budget, running) : ''}
    <div class="episode-choice-tools"><div>${selectButton('', scopeLabel, entries.length)}${checkButton}
    ${selectButton('-pending','选择待扫描',selectCatalogEpisodes({entries},'pending').length)}${selectButton('-ready','选择可下载',selectCatalogEpisodes({entries},'ready').length)}</div>
    ${managed ? `<span aria-live="polite">共 ${entries.length} 集 · 已选 ${checkedEntries.length} 集${dismissHint}</span>` : ''}</div>
    ${!entries.length ? '<p class="series-no-matches" role="status">没有符合筛选条件的单集</p>' : `<div class="episode-list">${entries.map(entry => {
      const status = entryMediaStatus(entry);
      const statusLabel = status === 'needs-user' && accessPending(entry, catalog) ? '权限待核实' : mediaLabels[status];
      const number = `第 ${Number(entry.ordinal)} 集`;
      const title = String(entry.title || '').trim();
      const titleAlreadyNumbered = new RegExp('^第\\s*0*' + Number(entry.ordinal) + '\\s*集').test(title);
      const label = titleAlreadyNumbered ? title : [number, title].filter(Boolean).join(' · ');
      const receipt = receipts?.get(entry.episodeKey);
      const thumb = thumbnails[entry.episodeKey];
      const code = String(Number(entry.ordinal) || 0).padStart(2, '0');
      const preview = thumb
        ? `<span class="episode-frame" aria-hidden="true"><img src="${esc(thumb)}" alt="" loading="lazy"><b>E${code}</b><i></i></span>`
        : `<b class="episode-code" aria-hidden="true">E${code}</b>`;
      return `<label class="episode-entry${thumb ? '' : ' is-compact'}" data-media-status="${status}"${entry.scanMessage ? ` title="${esc(entry.scanMessage)}"` : ''}><input type="checkbox" data-episode-scan="${esc(id)}" value="${esc(entry.episodeKey)}" ${checked.has(entry.episodeKey) ? 'checked' : ''} id="ep-check-${esc(id)}-${esc(entry.episodeKey)}">${preview}<span><strong title="${esc(label)}">${esc(label)}</strong><small>${statusLabel}${receipt ? `<span class="episode-coin-receipt">${esc(receipt.label)}</span>` : ''}</small></span></label>`;
    }).join('')}</div>`}
    ${singleEpisode?.scanMessage ? `<p class="episode-scan-note" role="status">${esc(singleEpisode.scanMessage)}${accessPending(singleEpisode, catalog) ? ' · 账号权限待核实' : ''}</p>` : ''}
    ${receiptNote && !singleEpisode.scanMessage?.includes(receiptNote) ? `<p class="episode-scan-note" role="status">第 ${Number(singleEpisode.ordinal)} 集 · ${esc(receiptNote)}</p>` : ''}
    <div class="episode-actions"${noneSelected && !discovery ? ' hidden' : ''}>${seriesId ? '' : `<button class="btn btn-secondary" data-action="catalog-scan" data-id="${esc(id)}" ${scanDisabled ? 'disabled' : ''}>扫描所选集</button>`}<button class="btn btn-primary" data-action="${action}-download${seriesId ? '-selected' : ''}" data-id="${esc(id)}" ${downloadDisabled ? 'disabled' : ''}>下载所选集</button>${seriesId ? singleExport : archiveLink}${dismissButton}</div>
    ${seriesId ? (archiveLink ? `<div class="episode-archive"><small>本剧已保存 ${Number(catalog.downloadedCount)} 集 · 勾选一集可导出原件</small>${archiveLink}</div>` : '') : `<small>目录只包含本次实际观察到的页面。每集下载一个已确认的主媒体；扫描时已知需要权限的集会跳过，请先在授权窗口解锁。${archive ? 'ZIP 包含已保存原件与缺失清单。' : ''}</small>`}</div></details>`;
  return discovery ? discoveryCard(scan, catalog, details, siteName) : details;
}

import {escapeHtml as esc, icon} from './ui.js';
import {hasOriginal} from './version-view.js';
import {reservedAssets} from './intake-results.js';

export const intakeDestination = (scope, scanId) => ({
  page: ['site', 'series'].includes(scope) ? 'series' : 'results', scanId,
});

export function homePendingAssets(state, {query = '', page = 1} = {}) {
  const grouped = new Set();
  for (const show of state.series || []) {
    for (const id of show.artwork?.assetIds || []) grouped.add(id);
    for (const entry of show.catalog?.entries || []) {
      for (const id of [entry.downloadAssetId, ...(entry.assetIds || [])]) if (id) grouped.add(id);
    }
  }
  const term = query.trim().toLocaleLowerCase();
  const all = (state.assets || []).filter(asset => !asset.dismissedFromDiscovery && !hasOriginal(asset) && !grouped.has(asset.id));
  const filtered = all.filter(asset => !term || String(asset.title || '').toLocaleLowerCase().includes(term));
  const pages = Math.max(1, Math.ceil(filtered.length / 12));
  page = Math.min(pages, Math.max(1, Math.floor(Number(page) || 1)));
  return {items:filtered.slice((page - 1) * 12, page * 12), total:filtered.length, allCount:all.length, page, pages};
}

export function renderHomePending(state, view = {}) {
  const data = homePendingAssets(state, view);
  if (!data.allCount) return '';
  const selected = new Set(view.selected || []);
  const reserved = reservedAssets(state);
  const picked = data.items.filter(asset => selected.has(asset.id));
  const downloadable = picked.filter(asset => !asset.locked && !asset.unsupported && !reserved.has(asset.id));
  return `<details class="home-pending home-section" data-home-pending ${view.open ? 'open' : ''}>
    <summary><span><strong>其他待下载素材</strong><small>${data.allCount} 项 · 单集、短视频和未归属剧集的图片</small></span>${icon('chevronDown',16)}</summary>
    <div class="home-pending-tools"><input class="input" id="home-pending-query" type="search" placeholder="搜索素材名称" aria-label="搜索其他待下载素材" value="${esc(view.query || '')}">
      <button class="btn btn-secondary" data-action="home-pending-select">${data.items.length && picked.length === data.items.length ? '取消本页选择' : '全选本页'}</button>
      <button class="btn btn-primary" data-action="home-pending-download" ${downloadable.length ? '' : 'disabled'}>下载所选${downloadable.length ? `（${downloadable.length}）` : ''}</button>
      <button class="btn btn-ghost" data-action="home-pending-remove" ${picked.length ? '' : 'disabled'}>移除所选</button></div>
    <div class="home-pending-list">${data.items.map(asset => {
      const busy = reserved.has(asset.id);
      const caption = asset.locked ? '需站点权限' : asset.unsupported ? '暂不支持' : busy ? '已在队列' : asset.kind === 'image' ? '图片' : '视频';
      return `<div class="home-pending-row"><input type="checkbox" data-home-asset-check="${esc(asset.id)}" aria-label="选择 ${esc(asset.title || '素材')}" ${selected.has(asset.id) ? 'checked' : ''}>
        <button class="home-pending-title" data-action="detail" data-id="${esc(asset.id)}">${icon(asset.kind === 'image' ? 'image' : 'video',16)}<span>${esc(asset.title || '未命名素材')}</span></button><small>${caption}</small>
        ${asset.locked ? `<button class="btn btn-ghost" data-action="auth-site" data-id="${esc(asset.siteId)}">授权</button>` : `<button class="btn btn-ghost" data-action="download-one" data-id="${esc(asset.id)}" ${busy || asset.unsupported ? 'disabled' : ''}>下载</button>`}</div>`;
    }).join('') || '<p class="panel-empty">没有符合搜索条件的素材。</p>'}</div>
    ${data.pages > 1 ? `<nav class="home-pending-paging" aria-label="其他素材分页"><button class="btn btn-ghost" data-action="home-pending-page" data-value="${data.page - 1}" ${data.page === 1 ? 'disabled' : ''}>上一页</button><span>${data.page} / ${data.pages}</span><button class="btn btn-ghost" data-action="home-pending-page" data-value="${data.page + 1}" ${data.page === data.pages ? 'disabled' : ''}>下一页</button></nav>` : ''}
  </details>`;
}

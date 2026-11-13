import {escapeHtml as esc, icon} from './ui.js';

export const LIBRARY_PAGE_SIZE = 24;

export function librarySearchIds(groups, query) {
  const ids = new Set();
  for (const group of groups) {
    const groupMatch = [group.title, group.seriesKey, group.siteName]
      .some(value => String(value || '').toLowerCase().includes(query));
    for (const {asset, ordinal, episodeTitle} of group.items) {
      if (groupMatch || [episodeTitle, ordinal ? `第 ${ordinal} 集` : '', ordinal ? `第${ordinal}集` : '']
        .some(value => String(value || '').toLowerCase().includes(query))) ids.add(asset.id);
    }
  }
  return ids;
}

// Keep the catalog's episode order by default. Explicit file sorts apply within each show.
export function libraryVisibleGroups(groups, assets, sort = 'episode') {
  const ranks = new Map(assets.map((asset, index) => [asset.id, index]));
  return groups.flatMap(group => {
    const items = group.items.filter(({asset}) => ranks.has(asset.id));
    if (!items.length) return [];
    if (sort !== 'episode') items.sort((a,b) => ranks.get(a.asset.id) - ranks.get(b.asset.id));
    return [{...group, items}];
  });
}

export function renderLibraryGroups(groups, ui, renderAsset) {
  const titles = new Map();
  for (const group of groups) titles.set(group.title, (titles.get(group.title) || 0) + 1);
  return `<div class="library-groups">${groups.map(group => {
    const section = ui.librarySections.get(group.id);
    const open = section?.open ?? (groups.length === 1 || Boolean(ui.q.trim()));
    const shown = section?.shown || LIBRARY_PAGE_SIZE;
    const visible = open ? group.items.slice(0, shown) : [];
    const picked = group.items.filter(({asset}) => ui.selected.has(asset.id)).length;
    const picture = group.id === 'other-media';
    const site = [group.siteName, titles.get(group.title) > 1 && group.seriesKey].filter(Boolean).join(' · ');
    const title = esc(group.title), id = esc(group.id);
    const count = `${group.items.length} ${picture ? '份素材' : '个视频'}`;
    return `<section class="library-group" data-library-group="${id}">
      <div class="library-group-head"><button type="button" class="library-group-toggle" id="library-toggle-${id}" data-action="library-toggle-group" data-id="${id}" aria-expanded="${open}" aria-controls="library-items-${id}">
        <span class="library-group-symbol">${icon(picture ? 'image' : 'film',18)}</span><span class="library-group-label"><strong>${title}</strong>${site ? `<small>${esc(site)}</small>` : ''}</span>
        <span class="library-group-count">${count}</span>${icon('chevronRight',15)}</button>
      <button type="button" id="library-pick-${id}" class="library-group-pick" data-action="library-select-group" data-id="${id}" aria-label="${picked === group.items.length ? '取消选择' : '选择'}${title}的全部筛选结果" aria-pressed="${picked === group.items.length}">${picked === group.items.length ? '取消全选' : picked ? `已选 ${picked} · 全选` : '全选'}</button></div>
      <div class="library-group-body" id="library-items-${id}" ${open ? '' : 'hidden'}>${open ? `<div class="${ui.view === 'list' ? 'asset-list' : 'asset-grid'}">${visible.map(item => renderAsset(item.asset, item)).join('')}</div>` : ''}${open && shown < group.items.length ? `<button type="button" class="btn btn-ghost library-more" data-action="library-more" data-id="${id}">继续显示 ${Math.min(group.items.length-shown, LIBRARY_PAGE_SIZE)} 项 · 已显示 ${shown} / ${group.items.length}${icon('chevronDown',14)}</button>` : ''}</div>
    </section>`;
  }).join('')}</div>`;
}

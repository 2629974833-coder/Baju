// Both media views use the same catalogue and stable IDs, independent of display names.
export function groupLocalAssets(state, assets) {
  const visible = new Map(assets.map(asset => [asset.id, asset]));
  const sites = new Map((state.sites || []).map(site => [site.id, site.name || site.domain]));
  const groups = new Map(), membership = new Map();
  for (const series of state.series || []) {
    const catalog = series.catalog || {};
    const group = {id:series.id, title:series.customTitle || series.title || catalog.title || catalog.seriesKey || '未命名剧集',
      siteId:series.siteId, siteName:sites.get(series.siteId) || series.siteId || '', seriesKey:catalog.seriesKey || '', items:[]};
    groups.set(group.id, group);
    for (const entry of catalog.entries || []) {
      for (const id of new Set([entry.downloadAssetId, ...(entry.assetIds || [])])) {
        const asset = visible.get(id);
        if (asset?.kind !== 'video' || asset.siteId !== series.siteId) continue;
        const primary = id === entry.downloadAssetId;
        const prior = membership.get(id);
        if (!prior || (primary && !prior.primary)) membership.set(id, {group, entry, primary});
        else if (prior.primary === primary && (group !== prior.group || entry !== prior.entry)) prior.ambiguous = true;
      }
    }
  }
  const otherGroup = (id,title) => ({id,title,siteId:'',siteName:'',seriesKey:'',items:[]});
  const unassigned = otherGroup('unassigned-video','未归集视频'), other = otherGroup('other-media','其他素材');
  for (const asset of visible.values()) {
    const match = membership.get(asset.id);
    const known = match && !match.ambiguous;
    const fallback = asset.kind === 'video' ? unassigned : other;
    const group = known ? match.group : fallback;
    const entry = known ? match.entry : null;
    group.items.push({asset, ordinal:Number.isFinite(entry?.ordinal) ? entry.ordinal : null, episodeTitle:entry?.title || ''});
  }
  for (const group of groups.values()) group.items.sort((a,b)=>(a.ordinal ?? Infinity)-(b.ordinal ?? Infinity));
  return [...groups.values(),unassigned,other].filter(g=>g.items.length);
}

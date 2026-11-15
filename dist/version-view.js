import { escapeHtml as esc, formatSize, icon } from './ui.js';
import {renderMediaMenu} from './media-menu.js';

export function hasOriginal(asset) {
  return ['downloaded', 'processed'].includes(asset?.status) && ['image', 'video'].includes(asset?.kind);
}

export function processedVersions(asset) {
  return (asset?.versions || []).filter(version => version.id && version.id !== 'original'
    && version.kind === 'processed' && version.status === 'completed' && version.fileUrl);
}

export function matchesFileCollection(asset, collection = 'all') {
  if (!hasOriginal(asset)) return false;
  if (collection === 'processed') return processedVersions(asset).length > 0;
  if (collection === 'original') return processedVersions(asset).length === 0;
  if (collection === 'favorites') return Boolean(asset.favorite);
  return true;
}

export function libraryFilePresentation(asset, collection = 'all') {
  const versions = processedVersions(asset);
  const latest = collection === 'processed' ? versions.at(-1) : null;
  return {
    versionId: latest?.id || 'original',
    versionCount: versions.length,
    thumbnail: latest && asset.kind === 'image' ? versionFileUrl(asset, latest.id) : asset.thumbnail,
  };
}

export function versionFileUrl(asset, versionId = 'original') {
  if (!hasOriginal(asset)) return null;
  const base = `/api/assets/${encodeURIComponent(asset.id)}`;
  if (versionId === 'original') return `${base}/file`;
  return processedVersions(asset).some(version => version.id === versionId)
    ? `${base}/versions/${encodeURIComponent(versionId)}/file` : null;
}

function media(asset, url, label, versionId = 'original') {
  return `<figure class="version-media"><figcaption>${esc(label)}</figcaption>${asset.kind === 'video'
    ? `<video src="${url}" controls playsinline controlslist="nodownload noplaybackrate noremoteplayback" disablepictureinpicture disableremoteplayback preload="metadata" aria-label="${esc(label)}"></video>${renderMediaMenu(asset, versionId)}`
    : `<img src="${url}" alt="${esc(label)}">`}</figure>`;
}

export function renderVersionWorkspace(asset, { versionId = 'original', compare = false } = {}) {
  if (!hasOriginal(asset)) return '';
  const versions = processedVersions(asset);
  const selected = versions.find(version => version.id === versionId);
  const current = selected?.id || 'original';
  const originalUrl = versionFileUrl(asset);
  const currentUrl = versionFileUrl(asset, current);
  const comparison = Boolean(selected && compare);
  const coverage = selected?.removalMethod === 'chunlou-glyph' && Number.isInteger(selected.glyphFrames) && Number.isInteger(selected.totalFrames)
    ? ` · 识别并修复 ${selected.glyphFrames} / ${selected.totalFrames} 帧` : '';
  const title = selected?.label || '原始文件';
  return `<section class="version-workspace"><div class="version-toolbar"><label>查看版本<select class="select" data-real-version data-id="${esc(asset.id)}" aria-label="查看文件版本"><option value="original" ${current === 'original' ? 'selected' : ''}>原始文件</option>${versions.map((version, index) => `<option value="${esc(version.id)}" ${current === version.id ? 'selected' : ''}>版本 ${index + 1} · ${esc(version.label || '处理成品')}</option>`).join('')}</select></label><button class="btn btn-secondary" data-action="compare-version" data-id="${esc(asset.id)}" aria-pressed="${comparison}" ${selected ? '' : 'disabled'}>${icon('columns', 14)}${comparison ? '只看当前版本' : '对比原件'}</button><a class="btn btn-primary" href="${currentUrl}?download=true" download>${icon('download', 14)}${selected ? '保存此版本' : '保存原件'}</a></div><div class="${comparison ? 'version-comparison' : 'version-single'}">${comparison ? media(asset, originalUrl, '原始文件') : ''}${media(asset, currentUrl, title, current)}</div>${selected ? `<p class="version-meta">${Number(selected.width) || 0} × ${Number(selected.height) || 0} · ${formatSize((Number(selected.sizeBytes) || 0) / 1024 ** 2)} · ${esc(selected.createdAt || '')}${coverage}</p>` : ''}<p class="version-preservation">${icon('shield', 13)}原件始终保留，处理结果另存为独立版本。${comparison && asset.kind === 'video' ? '两侧视频可分别播放检查。' : ''}</p></section>`;
}

export function renderVersionRecords(asset) {
  const versions = processedVersions(asset);
  if (!versions.length) return '';
  return `<section class="version-records"><h3>处理记录 · ${versions.length} 个版本</h3>${versions.map((version, index) => `<div class="version-record"><div><strong>版本 ${index + 1} · ${esc(version.label || '处理成品')}</strong><span>${esc(version.createdAt || '')} · ${formatSize((Number(version.sizeBytes) || 0) / 1024 ** 2)}</span></div><a class="btn btn-ghost" href="${versionFileUrl(asset, version.id)}?download=true" download>保存版本 ${index + 1}</a></div>`).join('')}</section>`;
}

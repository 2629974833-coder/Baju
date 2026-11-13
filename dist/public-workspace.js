import {escapeHtml as esc, icon} from './ui.js';
import {hasOriginal, versionFileUrl} from './version-view.js';

export const publicWorkspace = state => state?.mode === 'real' && state.publicMode === true;
const privatePages = new Set(['process', 'automation', 'settings']);
export const workspacePage = (state, page) => publicWorkspace(state) && privatePages.has(page) ? 'home' : page;

export function renderWorkspaceNotice(state) {
  if (!publicWorkspace(state)) return '';
  const hours = Number.isFinite(state.retentionHours) && state.retentionHours > 0 ? state.retentionHours : 24;
  return `<aside class="public-workspace-note" data-workspace-notice><span>${icon('clock',16)}<strong>你的临时工作区</strong><small>免注册</small></span><p>${hours} 小时未使用后清理，下载完成后请保存到本机。<br>清除浏览器数据或换浏览器会开启新空间。</p></aside>`;
}

export function saveOriginalLink(asset, block = false) {
  if (!hasOriginal(asset)) return '';
  return `<a class="btn btn-primary${block ? ' btn-block' : ''}" href="${versionFileUrl(asset)}?download=true" download>${icon('download',15)}保存到本机</a>`;
}

export function renderPublicPreview(asset) {
  if (!hasOriginal(asset)) return '';
  const url = versionFileUrl(asset);
  return `<section class="version-workspace public-file-preview"><figure class="version-media"><figcaption>原始文件</figcaption>${asset.kind === 'video'
    ? `<video src="${url}" controls preload="metadata" aria-label="${esc(asset.title)}"></video>`
    : `<img src="${url}" alt="${esc(asset.title)}">`}</figure></section>`;
}

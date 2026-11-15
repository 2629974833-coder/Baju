import {escapeHtml as esc} from './ui.js';

export function renderMediaMenu(asset, versionId = 'original') {
  if (asset.kind !== 'video' || !['downloaded', 'processed'].includes(asset.status)) return '';
  const attrs = `data-id="${esc(asset.id)}" data-version="${esc(versionId)}"`;
  return `<details class="menu media-menu"><summary aria-label="视频更多操作" title="更多操作">⋯</summary><div class="menu-pop" role="menu">
    <button role="menuitem" data-action="media-share" ${attrs}>分享给微信好友</button>
    <button role="menuitem" data-action="media-save" ${attrs}>保存到手机相册</button>
    <button role="menuitem" data-action="media-options" ${attrs}>无水印选项</button>
  </div></details>`;
}

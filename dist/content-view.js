import { escapeHtml as esc } from './ui.js';

export const contentKinds = [['preview', '预览 / 预告'], ['episode', '正片候选'], ['audio', '独立音轨'], ['unknown', '待确认']];

export function classificationLabel(value) {
  if (value?.kind === 'episode' && value.source === 'manual') return '正片（人工）';
  return contentKinds.find(([kind]) => kind === value?.kind)?.[1] || '待确认';
}

export function matchesContent(asset, filter) {
  return filter === 'all' || (asset.kind === 'video' && (asset.classification?.kind || 'unknown') === filter);
}

export function renderClassification(asset) {
  if (asset.kind !== 'video') return '';
  const value = asset.classification || {kind:'unknown', confidence:'low', source:'automatic', reasons:[]};
  const confidence = {high:'高', medium:'中', low:'低'}[value.confidence] || '低';
  return `<section class="content-classification"><div class="row"><strong>${esc(classificationLabel(value))}</strong><span class="badge badge-gray">${value.source === 'manual' ? '人工分类' : `自动识别 · 置信度${confidence}`}</span></div><ul>${(value.reasons || []).map(reason => `<li>${esc(reason)}</li>`).join('')}</ul><form id="classification-form" data-id="${esc(asset.id)}"><label for="content-classification">修正内容类型</label><div class="row"><select class="input" id="content-classification" name="classification">${contentKinds.map(([kind, label]) => `<option value="${kind}" ${value.kind === kind ? 'selected' : ''}>${kind === 'episode' ? '正片（人工确认）' : label}</option>`).join('')}<option value="automatic">恢复自动识别</option></select><button class="btn btn-secondary" type="submit">保存分类</button></div></form></section>`;
}

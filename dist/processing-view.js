import { escapeHtml as esc, formatSize, icon } from './ui.js';
import { groupLocalAssets } from './asset-series.js';
import { motionControls, validateMotion } from './motion-editor.js';
import { hasOriginal, processedVersions, versionFileUrl } from './version-view.js';

export const PROCESSING_DEFAULTS = Object.freeze({
  mode: 'add', text: 'FRAMEKEEP', position: 'bottom-right', opacity: 65, fontSize: 4,
  region: Object.freeze({ x: 70, y: 80, width: 25, height: 15 }),
});
const positions = [['bottom-right', '右下角'], ['bottom-left', '左下角'], ['top-right', '右上角'], ['top-left', '左上角']];
const modes = [['add', '加水印'], ['remove', '去水印'], ['both', '去除后加水印']];
const statuses = { queued: '等待处理', running: '正在处理', paused: '已暂停', completed: '已完成', failed: '处理失败', cancelled: '已取消' };
const clamp = value => Math.max(0, Math.min(100, Number(value) || 0));
const localAssets = state => (state.assets || []).filter(hasOriginal);

// A transfer from the library may deliberately span several shows or include images.
export function studioBatch(state, {groupId, previewId, selectedIds = [], incomingIds = []} = {}) {
  const groups = groupLocalAssets(state, localAssets(state));
  const incoming = new Set(incomingIds);
  const sourceGroups = incoming.size ? groups.filter(group => group.items.some(({asset}) => incoming.has(asset.id))) : [];
  if (sourceGroups.length > 1) {
    groups.unshift({id: 'incoming-batch', title: '送入工作室的素材',
      items: sourceGroups.flatMap(group => group.items.filter(({asset}) => incoming.has(asset.id))
        .map(item => ({...item, episodeTitle:item.episodeTitle ? group.title + ' · ' + item.episodeTitle : ''})))});
  }
  const group = groups.find(group => group.id === groupId)
    || (sourceGroups.length > 1 ? groups[0] : groups.find(group => group.items.some(({asset}) => asset.id === previewId)))
    || groups[0];
  const items = group?.items || [];
  const selected = new Set(selectedIds);
  const checked = items.filter(({asset}) => selected.has(asset.id)).map(({asset}) => asset);
  const preview = items.find(({asset}) => asset.id === previewId)?.asset || checked[0] || items[0]?.asset;
  return {groups, group, items, selected: checked, selectedIds: checked.map(asset => asset.id), preview};
}

const isChunlouVideo = asset => asset?.kind === 'video' && asset.siteId === 'chunlou';
const glyphTargets = batch => batch.selected.length ? batch.selected : [batch.preview];

export function glyphRequest(state, batch) {
  return processingRequest(state, glyphTargets(batch).map(asset => asset?.id), {mode:'remove',method:'chunlou-glyph'});
}

function itemLabels(items) {
  const names = items.map(({asset, episodeTitle}) => episodeTitle || asset.title);
  const count = values => {
    const totals = new Map();
    for (const value of values) totals.set(value, (totals.get(value) || 0) + 1);
    return totals;
  };
  const byName = count(names);
  const labels = names.map((name, i) => byName.get(name) > 1 && items[i].asset.height
    ? name + ' · ' + items[i].asset.height + 'p' : name);
  const byLabel = count(labels);
  const indices = new Map();
  return new Map(items.map(({asset}, i) => {
    const label = labels[i];
    indices.set(label, (indices.get(label) || 0) + 1);
    return [asset.id, byLabel.get(label) > 1 ? label + ' · 素材 ' + indices.get(label) : label];
  }));
}

function previewPickers(batch, labels) {
  const {groups, group, items, preview} = batch;
  const titles = new Map();
  for (const entry of groups) titles.set(entry.title, (titles.get(entry.title) || 0) + 1);
  const choices = groups.map(entry => {
    const source = titles.get(entry.title) > 1 ? [entry.siteName, entry.seriesKey].filter(Boolean).join(' · ') : '';
    return `<option value="${esc(entry.id)}"${entry.id === group.id ? ' selected' : ''}>${esc(entry.title)}${source ? ' · ' + esc(source) : ''} (${entry.items.length})</option>`;
  }).join('');
  return `<div class="studio-preview-selectors"><label class="studio-preview-pick"><span>预览剧集</span><select class="select" data-processing-series aria-label="预览剧集">${choices}</select></label>
    <label class="studio-preview-pick"><span>预览素材</span><select class="select" data-processing-preview aria-label="预览处理素材">${items.map(({asset}) => `<option value="${esc(asset.id)}"${asset.id === preview.id ? ' selected' : ''}>${esc(labels.get(asset.id))}</option>`).join('')}</select></label></div>`;
}

function batchPicker(batch, labels) {
  const {group, items, preview} = batch;
  const selected = new Set(batch.selectedIds);
  const rows = items.map(({asset}) => {
    const inPreview = asset.id === preview.id;
    const name = labels.get(asset.id);
    const versions = processedVersions(asset).length;
    return `<div class="process-pick-item${selected.has(asset.id) ? ' is-selected' : ''}${inPreview ? ' is-previewing' : ''}">
      <input type="checkbox" data-action="toggle-process" data-id="${esc(asset.id)}"${selected.has(asset.id) ? ' checked' : ''} aria-label="选择处理 ${esc(name)}">
      <button type="button" class="studio-pick-preview" data-action="process-preview-asset" data-id="${esc(asset.id)}" aria-pressed="${inPreview}" aria-label="预览 ${esc(name)}" title="${esc(asset.title)}">
        <img src="${esc(asset.thumbnail || 'assets/favicon.svg')}" alt="" loading="lazy"><span><strong>${esc(name)}</strong><small>${asset.kind === 'video' ? '视频' : '图片'} · ${formatSize(Number(asset.sizeMB) || 0)}${versions ? ` · ${versions} 个成品` : ''}</small></span></button></div>`;
  }).join('');
  return `<aside class="process-picker"><div class="process-picker-title"><h3>本批素材</h3><span>${selected.size} / ${items.length}</span></div>
    <strong class="studio-batch-name">${esc(group.title)}</strong>
    <button class="btn btn-ghost processing-select-all" data-action="process-select-all">${selected.size === items.length ? '清空本组选择' : '全选本组素材'}</button>
    <div class="process-pick-list">${rows}</div><div class="process-picker-bottom">${icon('shield', 14)}每次从原件生成，原件不覆盖</div></aside>`;
}

function glyphShortcut(batch) {
  if (!batch.items.some(({asset}) => isChunlouVideo(asset))) return '';
  const targets = glyphTargets(batch);
  const eligible = targets.every(isChunlouVideo);
  const scope = batch.selected.length ? `所选 ${batch.selected.length} 项` : '当前预览';
  return `<section class="studio-glyph-tool"><button type="button" class="btn btn-secondary" data-action="process-chunlou-glyph"${eligible ? '' : ' disabled'}>${icon('wand', 15)}一键去飘动水印</button>
    <p>${eligible ? `${scope} · 结合前后帧精修飘字，另存为新成品。` : '仅适用于春楼视频，请调整本批勾选。'}</p></section>`;
}

function number(value, min, max, label) {
  const parsed = typeof value === 'boolean' || value === null || value === '' ? NaN : Number(value);
  if (!Number.isFinite(parsed) || parsed < min || parsed > max) throw new Error(`${label}请填写 ${min}–${max} 之间的数值。`);
  return parsed;
}

function optionsForRequest(values) {
  const options = { ...PROCESSING_DEFAULTS, ...values };
  if (!modes.some(([mode]) => mode === options.mode)) throw new Error('请选择有效的处理方式。');
  const result = { ...PROCESSING_DEFAULTS, mode: options.mode, region: null };
  if (options.mode !== 'remove') {
    if (!positions.some(([position]) => position === options.position)) throw new Error('请选择有效的水印位置。');
    const text = String(options.text ?? '').trim();
    if (!text || [...text].length > 80) throw new Error('水印文字请填写 1–80 个字符。');
    Object.assign(result, { text, position: options.position,
      opacity: number(options.opacity, 0, 100, '不透明度'), fontSize: number(options.fontSize, 1, 20, '字号比例') });
  }
  if (options.mode !== 'add') {
    const region = { ...PROCESSING_DEFAULTS.region, ...options.region };
    for (const key of ['x', 'y', 'width', 'height']) region[key] = number(region[key], 0, 100, '修复区域');
    if (!region.width || !region.height || region.x + region.width > 100 || region.y + region.height > 100) {
      throw new Error('修复区域必须有宽高，并完整位于画面内。');
    }
    result.region = region;
    if (options.motion !== undefined) result.motion = validateMotion(options.motion, region);
  }
  return result;
}

export function processingRequest(state, selectedIds, values = {}) {
  const ids = [...new Set(selectedIds)];
  if (!ids.length) throw new Error('请先选择要处理的素材。');
  if (ids.length > 1000) throw new Error('每次最多选择 1000 个素材。');
  const available = new Map(localAssets(state).map(asset => [asset.id, asset]));
  if (ids.some(id => !available.has(id))) throw new Error('只能处理已下载到本机的图片或视频，请更新选择。');
  if (values.method === 'chunlou-glyph') {
    if (values.mode !== 'remove' || !ids.every(id => isChunlouVideo(available.get(id)))) {
      throw new Error('自动去飘字仅适用于春楼视频。');
    }
    return {path:'/api/processes', body:{ids, options:{mode:'remove',method:'chunlou-glyph',revision:2}}};
  }
  if (values.mode !== 'add' && values.motion !== undefined && (ids.length !== 1 || available.get(ids[0]).kind !== 'video')) {
    throw new Error('移动轨迹只能应用到标记位置的单个视频。');
  }
  return { path: '/api/processes', body: { ids, options: optionsForRequest(values) } };
}

export function regionFromPoints(start, end) {
  const x = Math.min(clamp(start.x), clamp(end.x));
  const y = Math.min(clamp(start.y), clamp(end.y));
  const width = Math.abs(clamp(end.x) - clamp(start.x));
  const height = Math.abs(clamp(end.y) - clamp(start.y));
  if (width < 0.1 || height < 0.1) return null;
  return Object.fromEntries(Object.entries({ x, y, width, height }).map(([key, value]) => [key, Math.round(value * 100) / 100]));
}

export function updateProcessingPreview(container, options) {
  const watermark = container.querySelector('.processing-watermark');
  if (watermark) {
    const position = positions.some(([value]) => value === options.position) ? options.position : 'bottom-right';
    watermark.textContent = options.text || '';
    watermark.className = `processing-watermark position-${position}`;
    watermark.style.opacity = String(clamp(options.opacity) / 100);
    watermark.style.fontSize = `${Math.max(1, Math.min(20, Number(options.fontSize) || 4))}cqh`;
  }
  const mask = container.querySelector('[data-region-mask]');
  if (mask) for (const [key, css] of [['x', 'left'], ['y', 'top'], ['width', 'width'], ['height', 'height']]) {
    mask.style[css] = `${clamp(options.region?.[key])}%`;
  }
}

export function bindProcessingPreview(container) {
  const frame = container.querySelector('.processing-media-frame');
  const media = frame?.querySelector('img, video');
  const toggle = container.querySelector('[data-region-toggle]');
  let ready = false;
  const measure = () => {
    const width = media?.tagName === 'VIDEO' ? media.videoWidth : media?.naturalWidth;
    const height = media?.tagName === 'VIDEO' ? media.videoHeight : media?.naturalHeight;
    ready = width > 0 && height > 0;
    if (ready) {
      frame.style.aspectRatio = String(width / height);
      frame.style.maxWidth = `${width / height * 520}px`;
    }
    if (toggle) toggle.disabled = !ready;
  };
  media?.addEventListener('load', measure);
  media?.addEventListener('loadedmetadata', measure);
  media?.addEventListener('error', () => { ready = false; if (toggle) toggle.disabled = true; });
  measure();
  return () => ready;
}

export function bindProcessingRegion(container, onChange, movingSize = () => null) {
  const ready = bindProcessingPreview(container);
  const surface = container.querySelector('[data-region-surface]');
  const toggle = container.querySelector('[data-region-toggle]');
  const mask = container.querySelector('[data-region-mask]');
  if (!surface || !toggle || !mask) return;
  let start = null;
  let previousRegion = null;
  const point = event => {
    const rect = surface.getBoundingClientRect();
    return { x: (event.clientX - rect.left) / rect.width * 100, y: (event.clientY - rect.top) / rect.height * 100 };
  };
  const draggedRegion = end => {
    const size = movingSize();
    if (!size) return regionFromPoints(start, end);
    // Once a track has a reference box, each point moves its centre and keeps its size.
    const { width, height } = size;
    return { x: Math.round(100 * Math.min(100 - width, Math.max(0, end.x - width / 2))) / 100,
      y: Math.round(100 * Math.min(100 - height, Math.max(0, end.y - height / 2))) / 100, width, height };
  };
  const paint = region => {
    mask.hidden = false;
    for (const [key, css] of [['x', 'left'], ['y', 'top'], ['width', 'width'], ['height', 'height']]) mask.style[css] = `${region[key]}%`;
  };
  const cancelDrag = () => {
    if (start && previousRegion) paint(previousRegion);
    start = null;
  };
  toggle.addEventListener('click', () => {
    if (!ready()) return;
    cancelDrag();
    const active = toggle.getAttribute('aria-pressed') !== 'true';
    toggle.setAttribute('aria-pressed', String(active));
    toggle.textContent = active ? '完成画面选区' : '在画面上拖动选区';
    surface.classList.toggle('is-drawing', active);
    container.querySelector('video')?.pause();
  });
  surface.addEventListener('pointerdown', event => {
    if (!ready() || !surface.classList.contains('is-drawing') || event.button !== 0) return;
    previousRegion = Object.fromEntries(['x', 'y', 'width', 'height'].map(key => [key, clamp(container.querySelector(`[data-processing-region="${key}"]`)?.value)]));
    start = point(event);
    surface.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  surface.addEventListener('pointermove', event => {
    if (!start) return;
    const region = draggedRegion(point(event));
    if (region) paint(region);
  });
  surface.addEventListener('pointerup', event => {
    if (!start) return;
    const region = draggedRegion(point(event));
    if (!region) { cancelDrag(); return; }
    start = null;
    paint(region);
    for (const [key, value] of Object.entries(region)) {
      const input = container.querySelector(`[data-processing-region="${key}"]`);
      if (input) input.value = value;
    }
    onChange({ ...region });
  });
  surface.addEventListener('pointercancel', cancelDrag);
}

export function renderProcessingJobs(state) {
  const jobs = [...(state.processingJobs || [])].reverse();
  if (!jobs.length) return '<div class="processing-empty-jobs">尚无处理任务，选择素材并设置参数后开始。</div>';
  const button = (job, action, label) => `<button class="btn btn-ghost" data-action="processing-job-action" data-id="${esc(job.id)}" data-value="${action}">${label}</button>`;
  return `<div class="processing-job-list">${jobs.map(job => {
    const asset = (state.assets || []).find(item => item.id === job.assetId);
    const version = processedVersions(asset).find(item => item.id === job.versionId);
    const fileUrl = version && versionFileUrl(asset, version.id);
    let controls = '';
    if (['queued', 'running'].includes(job.status)) controls += button(job, 'pause', '暂停');
    if (job.status === 'paused') controls += button(job, 'resume', '重新开始');
    if (['failed', 'cancelled'].includes(job.status)) controls += button(job, 'retry', '重试');
    if (['queued', 'running', 'paused', 'failed'].includes(job.status)) controls += button(job, 'cancel', '取消');
    if (job.status === 'completed' && fileUrl) controls += `<button class="btn btn-ghost" data-action="detail" data-id="${esc(asset.id)}" data-version="${esc(version.id)}">查看成品</button><a class="btn btn-ghost" href="${fileUrl}?download=true" download>保存版本</a>`;
    controls += `<button type="button" class="btn btn-ghost" data-action="delete-processing-job" data-id="${esc(job.id)}">${icon('trash', 13)}删除任务</button>`;
    const glyph = job.options?.method === 'chunlou-glyph';
    const glyphLabel = job.options?.revision === 2 ? '春楼飘字精修' : '春楼飘字修复';
    const coverage = glyph && Number.isInteger(version?.glyphFrames) && version.totalFrames > 0
      ? `已修复 ${version.glyphFrames} / ${version.totalFrames} 帧` : '';
    const message = job.error || coverage || (job.status === 'paused' ? '继续时从原件重新处理，保留原件和已完成版本。' : ({ processing: '正在生成新版本', verifying: '正在校验处理结果', queued: '等待 CPU 处理位', completed: '已校验并保存独立版本' }[job.phase || job.status] || '等待服务更新进度'));
    return `<article class="processing-job"><div><strong>${esc(asset?.title || '素材已不可用')}</strong><span class="badge ${job.status === 'failed' ? 'badge-amber' : job.status === 'completed' ? 'badge-green' : 'badge-gray'}">${statuses[job.status] || '处理记录'}</span></div><div class="processing-job-progress"><progress max="100" value="${clamp(job.progress)}" aria-label="${esc(asset?.title || '处理任务')}处理进度"></progress><span>${Math.round(clamp(job.progress))}%</span></div><p>${glyph ? glyphLabel + ' · ' : ''}${esc(message)}</p><div class="processing-job-controls">${controls}</div></article>`;
  }).join('')}</div>`;
}

export function renderRealProcess(state, draft = {}) {
  const batch = studioBatch(state, draft);
  const {selected, preview} = batch;
  const {options = {}, tracks} = draft;
  const config = { ...PROCESSING_DEFAULTS, ...options, region: { ...PROCESSING_DEFAULTS.region, ...options.region } };
  const removing = ['remove', 'both'].includes(config.mode);
  const adding = ['add', 'both'].includes(config.mode);
  const track = tracks?.get(preview?.id);
  const moving = removing && preview?.kind === 'video' && track?.active;
  const correctSelection = selected.length === 1 && selected[0].id === preview?.id;
  if (moving) config.region = track.region;

  const position = positions.some(([value]) => value === config.position) ? config.position : 'bottom-right';
  const region = Object.fromEntries(Object.entries(config.region).map(([key, value]) => [key, clamp(value)]));
  const ratio = preview?.width > 0 && preview?.height > 0 ? preview.width / preview.height : 16 / 9;
  const opacity = clamp(config.opacity);
  const fontSize = Math.max(1, Math.min(20, Number(config.fontSize) || 4));
  const heading = `<header class="page-heading"><div class="page-heading-copy"><div class="eyebrow">处理</div><h1>工作室</h1><p>处理已下载的图片和视频，每次另存独立版本，原件不覆盖。</p></div><div class="page-heading-actions"><span class="badge badge-gray">${icon('cpu', 13)}CPU · 同时处理 1 项</span></div></header>`;
  const jobs = `<section class="processing-jobs"><div class="processing-jobs-heading"><h2>处理任务 <span data-processing-job-count>${(state.processingJobs || []).length}</span></h2><p>暂停或服务重启后，从原件重新处理。</p></div><div data-processing-jobs>${renderProcessingJobs(state)}</div></section>`;
  if (!preview) return `<section class="workflow-page real-processing">${heading}<div class="empty-state process-empty">${icon('wand', 30)}<h3>先下载一些素材</h3><p>本地原件下载完成后，可在这里选择图片与视频进行处理。</p><button class="btn btn-primary" data-action="navigate" data-page="discover">去发现素材</button></div>${jobs}</section>`;
  const labels = itemLabels(batch.items);
  const stage = `<div class="processing-preview-heading">${previewPickers(batch, labels)}${removing ? '<button type="button" class="btn btn-secondary" data-region-toggle aria-pressed="false">在画面上拖动选区</button>' : ''}</div><div class="processing-preview-area"><div class="processing-media-frame" data-processing-asset="${esc(preview.id)}" style="aspect-ratio:${ratio};max-width:${Math.round(ratio * 520)}px">${preview.kind === 'video' ? `<video src="${versionFileUrl(preview)}" controls preload="metadata" aria-label="${esc(preview.title)}的原件"></video>` : `<img src="${versionFileUrl(preview)}" alt="${esc(preview.title)}的原件">`}${removing ? `<div class="processing-region" data-region-mask style="left:${region.x}%;top:${region.y}%;width:${region.width}%;height:${region.height}%"><span>修复区域</span></div><div class="processing-region-surface" data-region-surface aria-hidden="true"></div>` : ''}${adding ? `<span class="processing-watermark position-${position}" style="opacity:${opacity / 100};font-size:${fontSize}cqh">${esc(config.text || '')}</span>` : ''}</div></div>${moving ? motionControls(track) : ''}<p class="processing-preview-note">${icon('info', 13)}配置预览仅显示位置，尚未执行处理。成品完成后可在资料库对比原件与新版本。</p>`;
  const corners = `<div class="corner-picker" role="radiogroup" aria-label="水印位置">${[['top-left', '左上'], ['top-right', '右上'], ['bottom-left', '左下'], ['bottom-right', '右下']].map(([value, label]) => `<label class="corner ${position === value ? 'active' : ''}" title="${label}"><input type="radio" name="real-processing-position" value="${value}" data-processing-field="position" ${position === value ? 'checked' : ''} aria-label="${label}"><i></i></label>`).join('')}</div>`;
  const removalPicker = preview.kind === 'video' ? `<label class="field"><span>水印位置</span>
    <select class="select" data-motion-mode aria-label="水印位置模式"><option value="fixed">固定位置</option><option value="moving"${moving ? ' selected' : ''}>随时间移动</option></select></label>` : '<strong>固定区域去除</strong>';
  const ready = moving ? correctSelection && track.points.length && !track.dirty : selected.length;
  const panel = `<section class="preset-panel"><div class="preset-title"><h3>处理方式</h3><span class="badge badge-gray">${moving ? '当前视频' : '批量统一应用'}</span></div><div class="preset-mode-group" role="group" aria-label="处理方式">${modes.map(([value, label]) => `<label class="preset-mode ${config.mode === value ? 'active' : ''}"><input type="radio" name="real-processing-mode" value="${value}" data-processing-field="mode" ${config.mode === value ? 'checked' : ''}>${label}</label>`).join('')}</div>${adding ? `<div class="processing-option-fields"><label class="field processing-text-field"><span>水印文字</span><input class="input" data-processing-field="text" value="${esc(config.text)}" maxlength="80" aria-label="水印文字"></label><div class="studio-position"><span class="studio-label">位置</span>${corners}<div class="studio-sliders"><label class="field"><span>不透明度 <output data-processing-output="opacity">${opacity}%</output></span><input class="range" type="range" min="0" max="100" step="1" data-processing-field="opacity" value="${esc(config.opacity)}" aria-label="水印不透明度"></label><label class="field"><span>字号 · 画面高度 <output data-processing-output="fontSize">${fontSize}%</output></span><input class="range" type="range" min="1" max="20" step="0.5" data-processing-field="fontSize" value="${esc(config.fontSize)}" aria-label="水印字号比例"></label></div></div></div>` : ''}${removing ? `<div class="processing-removal-fields">${removalPicker}<p>${moving ? '先框出水印大小并记录。之后定位到新时刻，在选区模式下点水印中心即可移动同尺寸的框。' : '可拖动画面选区或填写百分比。四周需留出边界，复杂背景可能留痕。'}</p><div class="processing-region-fields">${[['x', '左侧'], ['y', '顶部'], ['width', '宽度'], ['height', '高度']].map(([key, label]) => `<label class="field"><span>${label} %</span><input class="input" type="number" min="0" max="100" step="0.1" data-processing-region="${key}" value="${esc(config.region[key])}" aria-label="${label}位置（%）"></label>`).join('')}</div>${moving && !correctSelection ? '<button type="button" class="btn btn-secondary" data-motion-select-only>仅选择此视频进行处理</button>' : ''}</div>` : ''}<dl class="studio-output"><div><dt>编码</dt><dd>${preview.kind === 'video' ? 'H.264 · AAC，CPU 重新编码' : 'PNG，保留透明通道'}</dd></div><div><dt>尺寸</dt><dd>保持原尺寸</dd></div><div><dt>原件</dt><dd class="is-jade">不覆盖</dd></div></dl></section><div class="process-start"><div><strong>已选 ${selected.length} 份素材</strong><span>相同参数的已完成或活动任务会去重</span></div><button class="btn btn-primary btn-block" data-action="start-process" ${ready ? '' : 'disabled'}>${icon('wand', 15)}开始处理${selected.length ? ` ${selected.length} 项` : ''}</button></div>`;
  return `<section class="workflow-page real-processing">${heading}<div class="studio">${batchPicker(batch, labels)}<section class="studio-stage" aria-label="画布">${stage}</section><aside class="studio-inspector" aria-label="处理参数">${glyphShortcut(batch)}${panel}</aside></div>${jobs}</section>`;
}

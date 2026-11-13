import { escapeHtml as esc } from './ui.js';

const maxPoints = 32;
const centiseconds = value => Math.round(value * 100) / 100;

export function newMotionDraft(region) {
  return { active: false, time: 0, points: [], region: { ...region }, transition: 'linear', dirty: false };
}

export function motionPosition(points, time) {
  if (!points.length || time < points[0].time) return null;
  const next = points.findIndex(p => p.time > time);
  const from = next < 0 ? points.at(-1) : points[next - 1];
  if (next < 0 || points[next].transition === 'jump') return { x: from.x, y: from.y };
  const to = points[next];
  const progress = (time - from.time) / (to.time - from.time);
  return { x: centiseconds(from.x + (to.x - from.x) * progress), y: centiseconds(from.y + (to.y - from.y) * progress) };
}

export function setMotionPoint(points, time, region, transition) {
  const at = centiseconds(time);
  if (!Number.isFinite(at) || at < 0) throw new Error('请先定位原视频。');
  const point = { time: at, x: Number(region.x), y: Number(region.y), transition };
  const rest = points.filter(p => p.time !== at);
  if (rest.length >= maxPoints) throw new Error('这条轨迹已经有 32 个位置，可更新或移除已有位置。');
  return [...rest, point].sort((a, b) => a.time - b.time);
}

export function validateMotion(points, region) {
  if (!Array.isArray(points) || !points.length || points.length > maxPoints) throw new Error('请为视频记录 1–32 个水印位置。');
  return points.map((p, index) => {
    if (!p || !Number.isFinite(p.time) || p.time < 0 || (index && p.time <= points[index - 1].time)
        || !Number.isFinite(p.x) || !Number.isFinite(p.y) || p.x < 0 || p.y < 0
        || p.x + Number(region.width) > 100 || p.y + Number(region.height) > 100
        || !['linear', 'jump'].includes(p.transition)) throw new Error('请检查轨迹时间与位置，选区须位于画面内。');
    return { time: p.time, x: p.x, y: p.y, transition: p.transition };
  });
}

export function motionControls(track) {
  return `<div class="motion-editor" data-motion-editor>
    <div class="motion-timeline"><label>定位画面 <span><input class="input" type="number" data-motion-time min="0" step="0.01" value="${esc(track.time.toFixed(2))}" aria-label="定位时间（秒）" disabled> 秒</span></label>
      <input type="range" data-motion-seek min="0" step="0.01" value="${esc(track.time)}" disabled aria-label="定位原视频时间"></div>
    <div class="motion-toolbar"><select class="select" data-motion-transition aria-label="到达此位置的方式"><option value="linear">平滑移动</option><option value="jump"${track.transition === 'jump' ? ' selected' : ''}>瞬间换位</option></select>
      <button type="button" class="btn btn-secondary" data-motion-record disabled>记录此处位置</button></div>
    <div class="motion-points" data-motion-points aria-label="已记录的位置"></div>
    <p data-motion-note role="status"></p></div>`;
}

export function bindMotionEditor(container, draft, { selectionOK, busy, notice }) {
  const video = container.querySelector('.processing-media-frame video');
  if (!video) return;
  const editor = container.querySelector('[data-motion-editor]');
  const slider = editor?.querySelector('[data-motion-seek]');
  const seconds = editor?.querySelector('[data-motion-time]');
  const record = editor?.querySelector('[data-motion-record]');
  const points = editor?.querySelector('[data-motion-points]');
  const transition = editor?.querySelector('[data-motion-transition]');
  const mask = container.querySelector('[data-region-mask]');
  const start = container.querySelector('[data-action="start-process"]');
  let restored = false;
  function list() {
    if (!points) return;
    points.innerHTML = draft.points.map((p,i) => `<span class="motion-chip"><button type="button" data-motion-goto="${p.time}" aria-label="定位到 ${p.time} 秒">${p.time.toFixed(2)} 秒${i > 0 ? (p.transition === 'jump' ? ' · 跳' : ' · 移') : ''}</button><button type="button" data-motion-remove="${p.time}" aria-label="移除 ${p.time} 秒的位置">×</button></span>`).join('');
  }
  function sync() {
    if (!editor || !restored) return;
    const at = draft.time;
    slider.value = String(at);
    if (document.activeElement !== seconds) seconds.value = at.toFixed(2);
    const current = draft.points.find(p => p.time === at);
    if (current && !draft.dirty) transition.value = draft.transition = current.transition;
    const position = motionPosition(draft.points, at);
    mask.hidden = !!draft.points.length && !position && !draft.dirty && !container.querySelector('.is-drawing');
    if (!draft.dirty && position) {
      for (const [k,css] of [['x','left'],['y','top']]) {
        draft.region[k] = position[k];
        mask.style[css] = position[k] + '%';
        container.querySelector(`[data-processing-region="${k}"]`).value = position[k];
      }
    }
    const end = video.ended || at >= video.duration;
    record.disabled = !slider.max || end;
    record.textContent = current ? '更新此处位置' : '记录此处位置';
    const note = editor.querySelector('[data-motion-note]');
    note.textContent = end ? '请在片尾之前标记。' : draft.dirty ? '当前画框尚未记录。' : draft.points.length
      ? '已记录 ' + draft.points.length + ' 个位置。首个位置起生效，最后一个位置保持到片尾。'
      : '暂停并框选水印，记录第一个位置。';
    for (const button of points.querySelectorAll('[data-motion-goto]')) button.setAttribute('aria-pressed', String(Number(button.dataset.motionGoto) === at));
    if (start) start.disabled = busy() || !selectionOK() || draft.dirty || !draft.points.length;
  }
  function seek(time) {
    if (!restored) return;
    video.pause();
    draft.dirty = false;
    draft.time = centiseconds(Math.min(time, video.duration));
    video.currentTime = draft.time;
    sync();
  }
  function ready() {
    if (!Number.isFinite(video.duration) || video.duration <= 0 || restored) return;
    restored = true;
    if (draft.time) video.currentTime = Math.min(draft.time, video.duration);
    if (!editor) return;
    slider.max = seconds.max = video.duration;
    slider.disabled = seconds.disabled = false;
    list();
    sync();
  }
  video.addEventListener('loadedmetadata', ready);
  ready();
  video.addEventListener('timeupdate', () => {
    if (!restored || !container.contains(video)) return;
    const time = centiseconds(video.currentTime);
    if (draft.time !== time) { draft.time = time; draft.dirty = false; }
    sync();
  });
  if (!editor) return;
  slider.addEventListener('input', () => seek(Number(slider.value)));
  seconds.addEventListener('change', () => {
    if (Number.isFinite(seconds.valueAsNumber)) seek(Math.max(0, seconds.valueAsNumber));
    seconds.value = draft.time.toFixed(2);
  });
  transition.addEventListener('change', () => {
    draft.transition = transition.value;
    draft.dirty = true;
    sync();
  });
  record.addEventListener('click', () => {
    try {
      video.pause();
      const revised = setMotionPoint(draft.points, video.currentTime, draft.region, draft.transition);
      validateMotion(revised, draft.region);
      if (revised.at(-1).time >= video.duration) throw new Error('请在片尾之前记录水印位置。');
      if (!(Math.round(video.videoWidth * Number(draft.region.width) / 100) >= 2
        && Math.round(video.videoHeight * Number(draft.region.height) / 100) >= 2)) throw new Error('请框选至少 2 × 2 像素的范围。');
      draft.points = revised;
      draft.time = centiseconds(video.currentTime);
      draft.dirty = false;
      list(); sync();
    } catch (error) { notice(error.message); }
  });
  points.addEventListener('click', event => {
    if (event.target.hasAttribute('data-motion-goto')) seek(Number(event.target.dataset.motionGoto));
    if (event.target.hasAttribute('data-motion-remove')) {
      draft.points = draft.points.filter(p => p.time !== Number(event.target.dataset.motionRemove));
      draft.dirty = false;
      list(); sync();
    }
  });
  // The drawing surface and numeric fields share the same draft and mask.
  return () => { video.pause(); draft.dirty = true; sync(); };
}

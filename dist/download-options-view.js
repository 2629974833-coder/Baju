import { icon, escapeHtml as esc } from "./ui.js";

const batchHeights = [480, 720, 1080, 1440, 2160, 4320];
const originalOptions = () => ({ maxHeight: null, audioLanguage: "auto" });
const sourceHeights = (capabilities) => [...new Set((capabilities?.heights || []).filter((height) => Number.isInteger(height) && height >= 144 && height <= 4320))].sort((a, b) => a - b);
const sourceTracks = (capabilities) => (capabilities?.audioTracks || []).filter((track) => track.language && track.language !== "auto");
const supportsSelection = (capabilities) => capabilities?.selectable === true && ["hls", "dash"].includes(capabilities.protocol);
const audioLabels = { auto: "默认音轨", und: "语言未标注", none: "无独立音轨" };
const audioSummary = (language) => audioLabels[String(language || "auto").toLowerCase()] || `音轨 ${language}`;
function audioTrackLabel(track) {
  const language = track.language.toLowerCase();
  const label = audioLabels[language] || track.language;
  const name = track.name || track.language;
  if (audioLabels[language] && name.toLowerCase() === language) return label;
  return `${name} · ${label}`;
}

export function downloadRequest(target, options) {
  if (target.jobId) return { path: `/api/jobs/${encodeURIComponent(target.jobId)}/action`, body: { action: "retry", options } };
  if (target.seriesId) return { path: `/api/series/${encodeURIComponent(target.seriesId)}/downloads`, body: { episodeKeys: target.episodeKeys, options } };
  if (target.scanId) return { path: `/api/scans/${encodeURIComponent(target.scanId)}/downloads`, body: { episodeKeys: target.episodeKeys, options } };
  return { path: "/api/downloads", body: { ids: target.ids, ...(options ? { options } : {}) } };
}

export function parseDownloadOptions(values, { batch = false, capabilities } = {}) {
  if (!batch && !supportsSelection(capabilities)) return originalOptions();
  const rawHeight = String(values.maxHeight ?? "").trim();
  const maxHeight = rawHeight ? Number(rawHeight) : null;
  if (maxHeight !== null && (!Number.isInteger(maxHeight) || maxHeight < 144 || maxHeight > 4320)) {
    throw new Error("请选择有效的最高画质。");
  }
  if (!batch && maxHeight !== null && !sourceHeights(capabilities).includes(maxHeight)) {
    throw new Error("此源未提供所选画质，请重新选择。");
  }
  const audioLanguage = String(values.audioLanguage || "auto").trim().toLowerCase();
  if (audioLanguage.length > 35 || !/^(?:auto|[a-z]{2,3}(?:-[a-z0-9]{2,8})*)$/.test(audioLanguage)) {
    throw new Error("请输入有效的音轨语言，如 zh、en、ja 或 zh-Hans；自动选择请填 auto。");
  }
  if (!batch && audioLanguage !== "auto" && !sourceTracks(capabilities).some((track) => track.language.toLowerCase() === audioLanguage)) {
    throw new Error("此源未提供所选音轨，请重新选择。");
  }
  return { maxHeight, audioLanguage };
}

export function renderDownloadOptions({ count = 1, title = "", status = "ready", capabilities, batch = false, error = "", values = {}, retry = false, busy = false } = {}) {
  const subtitle = title || `为 ${count} 项素材设置本次下载偏好`;
  const header = `<div class="modal-header"><div><h2>${retry ? "调整下载选项" : "下载选项"}</h2><p>${esc(subtitle)}</p></div><button class="icon-button" data-action="close-modal" aria-label="关闭弹窗">${icon("x", 19)}</button></div>`;
  const cancel = '<button type="button" class="btn btn-secondary" data-action="close-modal">取消</button>';
  if (status === "loading") {
    return `${header}<div class="modal-body download-options-state" role="status">${icon("refresh", 20)}<p>正在读取源站提供的画质与音轨…</p></div><div class="modal-footer">${cancel}</div>`;
  }
  if (status === "error") {
    return `${header}<div class="modal-body"><div class="download-options-error" role="alert"><strong>暂时无法读取下载选项</strong><p>${esc(error || "请重试，或明确选择按原始默认下载。")}</p></div><p class="download-options-note">尚未确认此源是否提供可选画质或音轨。按原始默认继续时，将使用源站最高画质与默认音轨。</p></div><div class="modal-footer download-options-footer">${cancel}<button type="button" class="btn btn-secondary" data-action="retry-download-options" ${busy ? "disabled" : ""}>重新读取</button><button type="button" class="btn btn-primary" data-action="download-default" ${busy ? "disabled" : ""}>按原始默认${retry ? "重试" : "下载"}</button></div>`;
  }
  let controls = '<div class="download-options-original"><strong>保留原件</strong><p>此来源没有可选择的画质或音轨，将保存源站提供的原始文件。</p></div>';
  if (batch || supportsSelection(capabilities)) {
    const heights = batch ? batchHeights : sourceHeights(capabilities);
    const heightOptions = heights.map((height) => `<option value="${height}" ${Number(values.maxHeight) === height ? "selected" : ""}>最高 ${height}p</option>`).join("");
    let heightNote = "从源站已有版本中选择，不会放大画面或转码。";
    if (!batch && !heights.length) {
      heightNote = capabilities.heights?.some((height) => Number(height) > 0)
        ? "此源暂无可设置的画质上限，将按原始画质下载。"
        : "源站未提供画质尺寸，将按原始默认选择。";
    }
    const tracks = sourceTracks(capabilities);
    const language = values.audioLanguage || "auto";
    let audioControl;
    if (batch) {
      audioControl = `<input class="input" id="download-audio" name="audioLanguage" value="${esc(language)}" maxlength="35" placeholder="auto / zh / en / ja" autocomplete="off" autocapitalize="off" spellcheck="false"><small>填写语言代码，如 zh、en、ja 或 zh-Hans；auto 使用默认音轨。</small>`;
    } else {
      const languages = new Set();
      const trackOptions = tracks.filter((track) => {
        const key = track.language.toLowerCase();
        if (languages.has(key)) return false;
        languages.add(key);
        return true;
      }).map((track) => `<option value="${esc(track.language)}" ${language.toLowerCase() === track.language.toLowerCase() ? "selected" : ""}>${esc(audioTrackLabel(track))}${track.default ? "（源站默认）" : ""}</option>`).join("");
      audioControl = `<select class="select" id="download-audio" name="audioLanguage"><option value="auto">自动 · 源站默认音轨</option>${trackOptions}</select>${tracks.length ? "" : '<small>未提供可选音轨，将使用源站默认音轨。</small>'}`;
    }
    controls = `<div class="download-options-fields"><div class="field"><label for="download-height">最高画质</label><select class="select" id="download-height" name="maxHeight"><option value="">原始最高 · 不限制</option>${heightOptions}</select><small>${heightNote}</small></div><div class="field"><label for="download-audio">音轨语言</label>${audioControl}</div></div>`;
  }
  const notice = "画质与音轨偏好仅适用于提供多版本的流媒体；直接文件和图片始终保留原件，不转码。";
  const batchNote = batch ? '<p class="download-options-note">这组偏好会逐项应用；可用版本与音轨以各素材源站为准，实际选择会显示在下载队列。</p>' : "";
  return `${header}<form id="download-options-form"><div class="modal-body">${controls}${capabilities?.message ? `<p class="download-options-note">${esc(capabilities.message)}</p>` : ""}${batchNote}<div class="modal-info">${icon("info", 17)}<span>${notice}</span></div><p id="download-options-error" class="form-error" role="alert"></p></div><div class="modal-footer">${cancel}<button class="btn btn-primary" type="submit" ${busy ? "disabled" : ""}>${icon("download", 16)}${retry ? "应用选项并重试" : "确认加入下载队列"}</button></div></form>`;
}

export function renderDownloadSelection(options, selection) {
  if (!options && !selection) return "";
  const lines = [];
  if (options) {
    const quality = options.maxHeight ? `最高 ${options.maxHeight}p` : "原始最高";
    const audio = audioSummary(options.audioLanguage);
    lines.push(`<p>偏好：${esc(quality)} · ${esc(audio)}</p>`);
  }
  let actual = "下载时确认";
  if (selection?.protocol === "http") actual = "保留原件（未应用画质 / 音轨偏好）";
  else if (selection) {
    const height = Number(selection.height) > 0 ? `${selection.height}p` : "尺寸未提供";
    const audio = audioSummary(selection.audioLanguage);
    actual = `${height} · ${audio}`;
  }
  lines.push(`<p>实际：${esc(actual)}</p>`);
  if (selection?.message) lines.push(`<p class="download-selection-note">${esc(selection.message)}</p>`);
  return `<div class="download-selection">${lines.join("")}</div>`;
}

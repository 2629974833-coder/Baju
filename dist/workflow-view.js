import { icon, escapeHtml as esc, formatSize } from "./ui.js";
import { renderDownloadSelection } from "./download-options-view.js";
import { renderRealProcess } from "./processing-view.js";
import { realAutomation, realRuleModal } from "./automation-view.js";
import { renderOperations } from "./operations-view.js";

const jobLabels = {
  running: "进行中",
  queued: "等待中",
  paused: "已暂停",
  failed: "需重试",
  "needs-user": "等待授权",
  completed: "已完成",
  cancelled: "已取消",
};
const presetDefaults = {
  mode: "add",
  text: "FRAMEKEEP",
  position: "bottom-right",
  opacity: 65,
};
const safeProgress = (value) => Math.max(0, Math.min(100, Number(value) || 0));
const badgeClass = (status) =>
  status === "completed" || status === "running"
    ? "badge-green"
    : ["failed", "needs-user"].includes(status)
      ? "badge-amber"
      : "badge-gray";
const siteName = (state, id) =>
  state.sites.find((site) => site.id === id)?.name || "未分类站点";

export function downloadCaption(job) {
  if (job.status === "completed") return "已保存到素材库";
  if (job.status === "cancelled") return "任务已取消";
  if (job.status === "queued") return "等待空闲任务位";
  if (job.status === "paused") return job.resumeMessage || "已暂停，继续时校验续传数据";
  if (job.status === "needs-user") return job.error || "请检查站点权限后重试";
  const phases = {refreshing: "正在刷新原页面的媒体链接", resuming: "正在校验并恢复下载", downloading: "正在传输媒体", verifying: "正在校验媒体文件", muxing: "正在合并音视频"};
  let caption = phases[job.phase] || "正在传输实际媒体文件";
  if (job.phase === "retrying") caption = `第 ${Number(job.retryCount) || 1} 次重试 · ${Math.ceil(Number(job.retryInSeconds) || 0)} 秒后继续`;
  if (Number(job.resumedBytes) > 0) caption += ` · 已复用 ${formatSize(job.resumedBytes / 1024 ** 2)}`;
  if (Number(job.totalSegments) > 0) caption += ` · 片段 ${Number(job.completedSegments) || 0}/${Number(job.totalSegments)}`;
  return caption;
}

function jobControls(job, asset, real = false) {
  const buttons = [];
  const action = (value, label, glyph) =>
    `<button class="icon-button" data-action="job-action" data-id="${esc(job.id)}" data-value="${value}" aria-label="${label} ${esc(asset.title)}" title="${label}">${icon(glyph, 16)}</button>`;
  if (job.status === "running" || job.status === "queued")
    buttons.push(action("pause", "暂停", "pause"));
  if (job.status === "paused") buttons.push(action("resume", "继续", "play"));
  if (["failed", "needs-user"].includes(job.status)) buttons.push(action("retry", "重试", "refresh"));
  if (real && job.type === "download" && asset.kind === "video" && ["failed", "needs-user"].includes(job.status))
    buttons.push(`<button class="btn btn-ghost" data-action="retry-download-selection" data-id="${esc(job.id)}">调整选项</button>`);
  if (real && job.status === "needs-user") buttons.push(`<button class="btn btn-ghost" data-action="auth-site" data-id="${esc(asset.siteId)}">管理权限</button><button class="btn btn-ghost" data-action="recover-job" data-id="${esc(job.id)}">重新扫描原页面</button>`);
  if (job.status === "completed" && job.type === "download")
    buttons.push(
      real ? `<button class="btn btn-ghost" data-action="detail" data-id="${esc(asset.id)}">查看文件</button><a class="btn btn-ghost" href="/api/assets/${encodeURIComponent(asset.id)}/file?download=true" download>保存原件 ${icon("download", 14)}</a>` : `<button class="btn btn-ghost queue-process-link" data-action="process-asset" data-id="${esc(asset.id)}">去处理 ${icon("arrowRight", 14)}</button>`,
    );
  if (!["completed", "cancelled"].includes(job.status))
    buttons.push(action("cancel", "移除任务", "x"));
  return buttons.join("");
}

function jobCard(state, job, real) {
  const asset = state.assets.find((item) => item.id === job.assetId);
  if (!asset) return "";
  const progress = safeProgress(job.progress);
  const tone = ["failed", "needs-user"].includes(job.status) ? "amber" : job.status === "completed" ? "done" : job.status === "running" ? "jade" : "muted";
  const caption = job.status === "failed"
    ? `<div class="queue-error">${esc(job.error || "连接中断，请重试")}</div>`
    : `<div class="queue-progress-caption">${esc(real ? downloadCaption(job) : job.status === "completed" ? "已保存到素材库" : job.status === "paused" ? "进度已保留" : job.status === "queued" ? "等待空闲任务位" : "模拟进度，不传输真实文件")}</div>`;
  return `<article class="job-card tone-${tone}"><img class="job-thumb" src="${esc(asset.thumbnail)}" alt="" loading="lazy"><div class="job-body"><div class="job-top"><span class="kind-tag tone-${job.type === "process" ? "blue" : "jade"}">${job.type === "process" ? "处理" : "下载"}</span><strong class="job-title">${esc(asset.title)}</strong><span class="badge ${badgeClass(job.status)}">${job.status === "running" ? '<span class="queue-badge-dot"></span>' : ""}${jobLabels[job.status] || esc(job.status)}</span><span class="job-pct">${progress}%</span></div><div class="queue-progress"><div class="queue-progress-track"><span class="${job.status === "failed" ? "is-error" : job.status === "paused" ? "is-paused" : ""}" style="width:${progress}%"></span></div></div><div class="job-foot">${caption}<span class="job-meta">${esc(siteName(state, asset.siteId))} · ${real && !asset.sizeMB ? "大小待确认" : formatSize(asset.sizeMB)}</span></div>${real && job.type === "download" ? renderDownloadSelection(job.downloadOptions, job.selection) : ""}</div><div class="queue-actions">${jobControls(job, asset, real)}</div></article>`;
}

export function renderQueue(state, filter = "all", side = "") {
  const real = state.mode === "real";
  const jobs = state.jobs;
  const running = jobs.filter((job) => job.status === "running").length;
  const queued = jobs.filter((job) => job.status === "queued").length;
  const completed = jobs.filter((job) => job.status === "completed").length;
  const failed = jobs.filter((job) => ["failed", "needs-user"].includes(job.status)).length;
  const filtered = jobs.filter((job) => filter === "all" || job.status === filter);
  const tabs = [
    ["all", "全部任务"],
    ["running", "进行中"],
    ["paused", "已暂停"],
    ["failed", "需重试"],
    ...(real ? [["needs-user", "等待授权"]] : []),
    ["completed", "已完成"],
  ];
  const groups = filter === "all"
    ? [
        ["需要处理", "amber", filtered.filter((job) => ["failed", "needs-user"].includes(job.status))],
        ["进行中", "jade", filtered.filter((job) => job.status === "running")],
        ["排队与暂停", "muted", filtered.filter((job) => ["queued", "paused"].includes(job.status))],
        ["已结束", "done", filtered.filter((job) => ["completed", "cancelled"].includes(job.status))],
      ]
    : [[jobLabels[filter] || "任务", "muted", filtered]];
  const list = groups.filter(([, , items]) => items.length).map(([title, tone, items]) => `<section class="job-group"><h2 class="job-group-title"><span class="group-dot tone-${tone}"></span>${title}<small>${items.length}</small></h2>${items.map((job) => jobCard(state, job, real)).join("")}</section>`).join("");
  return `<section class="workflow-page activity-page">
    <header class="page-heading"><div class="page-heading-copy"><div class="eyebrow">运行中</div><h1>活动</h1><p>${real ? "下载、处理与扫描都在这里 · 关闭网页不会中断后台任务" : "下载与处理任务都在这里 · 演示进度在本机浏览器中模拟"}</p></div><div class="page-heading-actions"><button class="btn btn-secondary" data-action="pause-all" ${!running && !queued ? "disabled" : ""}>${icon("pause", 15)}全部暂停</button><button class="btn btn-primary" data-action="new-scan">${icon("plus", 15)}拾取链接</button></div></header>
    <div class="queue-summary"><div><span class="queue-live-dot ${running ? "is-live" : ""}"></span><strong>${running}</strong><span>${real ? "正在下载" : "正在下载 / 处理"}</span></div><div><strong>${queued}</strong><span>等待执行</span></div><div><strong>${completed}</strong><span>已完成</span></div><div><strong class="${failed ? "queue-warning" : ""}">${failed}</strong><span>需要关注</span></div><span class="queue-summary-note">${icon("cpu", 14)}${real ? "真实下载 · 本机服务" : "演示任务 · 本机浏览器"}</span></div>
    <div class="queue-tabs" role="group" aria-label="任务状态筛选">${tabs.map(([value, name]) => `<button class="queue-filter ${filter === value ? "active" : ""}" data-action="queue-filter" data-value="${value}" aria-pressed="${filter === value}">${name}<span>${value === "all" ? jobs.length : jobs.filter((job) => job.status === value).length}</span></button>`).join("")}</div>
    <div class="activity-layout ${side ? "" : "is-single"}"><div class="activity-main">${list || `<div class="empty-state">${icon("download", 30)}<h3>这里还没有${filter === "all" ? "" : jobLabels[filter] || ""}任务</h3><p>在发现中挑选内容，加入下载。</p><button class="btn btn-secondary" data-action="navigate" data-page="discover">浏览素材 ${icon("arrowRight", 15)}</button></div>`}</div>${side ? `<aside class="activity-side">${side}</aside>` : ""}</div>
    <p class="workflow-footnote">${icon("info", 14)}${real ? "下载进度来自本地服务。站点权限、链接失效或受保护媒体可能导致任务失败，原因会显示在任务中。" : "当前为交互演示，任务进度不会触发真实下载或服务器处理。"}</p>
  </section>`;
}

export function renderProcess(
  state,
  selectedIds = [],
  preset = presetDefaults,
) {
  if (state.mode === "real") return renderRealProcess(state, { selectedIds, options: preset });
  const config = { ...presetDefaults, ...preset };
  const assets = state.assets.filter(
    (asset) => asset.status === "downloaded" || asset.status === "processed",
  );
  const selected = assets.filter((asset) => selectedIds.includes(asset.id));
  const preview = selected[0] || assets[0];
  const opacity = safeProgress(config.opacity);
  const validPositions = [
    "top-left",
    "top-right",
    "bottom-left",
    "bottom-right",
  ];
  const position = validPositions.includes(config.position)
    ? config.position
    : "bottom-right";
  const isRemoving = config.mode === "remove" || config.mode === "both";
  const isAdding = config.mode === "add" || config.mode === "both";
  return `<section class="workflow-page">
    <header class="page-heading"><div class="page-heading-copy"><div class="eyebrow">处理</div><h1>工作室</h1><p>一套处理模板，应用到多份素材。原件始终保留。</p></div><div class="page-heading-actions"><span class="badge badge-gray">${icon("cpu", 13)}CPU 优先</span></div></header>
    ${
      !assets.length
        ? `<div class="empty-state process-empty">${icon("wand", 34)}<h3>先下载一些素材</h3><p>下载完成后，即可在这里配置水印处理。</p><button class="btn btn-primary" data-action="navigate" data-page="discover">去发现素材 ${icon("arrowRight", 16)}</button></div>`
        : `
    <div class="process-layout"><aside class="process-picker"><div class="process-picker-title"><h3>选择素材</h3><span>${selected.length} / ${assets.length}</span></div><div class="process-pick-list">${assets.map((asset) => `<label class="process-pick-item ${selectedIds.includes(asset.id) ? "is-selected" : ""}"><input type="checkbox" data-action="toggle-process" data-id="${esc(asset.id)}" ${selectedIds.includes(asset.id) ? "checked" : ""} aria-label="选择处理 ${esc(asset.title)}"><img src="${esc(asset.thumbnail)}" alt="" loading="lazy"><span><strong>${esc(asset.title)}</strong><small>${asset.kind === "video" ? "视频" : "图片"} · ${formatSize(asset.sizeMB)}</small></span></label>`).join("")}</div><div class="process-picker-bottom">${icon("shield", 15)} 原件与处理版本分开保存</div></aside>
    <div class="process-main"><div class="process-preview-heading"><span>配置预览</span><span class="muted">${preview.kind === "video" ? "视频封面示意" : "图片示意"}</span></div><div class="process-preview"><img src="${esc(preview.thumbnail)}" alt="${esc(preview.title)} 的处理配置预览"><span class="process-preview-label">${icon(preview.kind === "video" ? "video" : "image", 13)} ${esc(preview.title)}</span>${isRemoving ? '<div class="process-removal-mask"><span>修复区域</span><i></i><i></i><i></i><i></i></div>' : ""}${isAdding ? `<span class="process-watermark position-${position}" style="opacity:${opacity / 100}">${esc(config.text || "FRAMEKEEP")}</span>` : ""}</div><p class="process-preview-caption">${icon("info", 13)} 此处仅预览水印位置和修复区域，未执行真实图像或视频处理。</p>
    <div class="preset-panel"><div class="preset-title"><h3>处理模板</h3><span class="badge badge-gray">自定义</span></div><div class="preset-mode-group" role="group" aria-label="处理方式">${[
      ["add", "加水印", "plus"],
      ["remove", "去水印", "wand"],
      ["both", "去除后加水印", "sliders"],
    ]
      .map(
        ([value, label, glyph]) =>
          `<label class="preset-mode ${config.mode === value ? "active" : ""}"><input type="radio" name="processing-mode" value="${value}" data-preset="mode" ${config.mode === value ? "checked" : ""}>${icon(glyph, 15)}${label}</label>`,
      )
      .join("")}</div>
    ${isRemoving ? `<div class="preset-removal-note"><div>${icon("wand", 16)}<strong>固定区域修复</strong><span class="badge badge-gray">右下角模板</span></div><p>按预设区域去除水印。复杂背景和移动水印需要人工检查；处理效果以实际接入后为准。</p></div>` : ""}
    ${
      isAdding
        ? `<div class="preset-fields"><label class="field preset-text-field"><span>水印文字</span><input class="input" data-preset="text" value="${esc(config.text)}" maxlength="40" placeholder="输入水印文字" aria-label="水印文字"></label><label class="field"><span>水印位置</span><select class="select" data-preset="position" aria-label="水印位置">${[
            ["bottom-right", "右下角"],
            ["bottom-left", "左下角"],
            ["top-right", "右上角"],
            ["top-left", "左上角"],
          ]
            .map(
              ([value, label]) =>
                `<option value="${value}" ${position === value ? "selected" : ""}>${label}</option>`,
            )
            .join(
              "",
            )}</select></label><label class="field preset-opacity-field"><span>不透明度 <strong>${opacity}%</strong></span><input type="range" min="10" max="100" step="5" value="${opacity}" data-preset="opacity" aria-label="水印不透明度"><small>文字可见度，不改变原始画面尺寸</small></label></div>`
        : ""
    }
    <div class="preset-output"><span>${icon("folder", 15)} 成品库 / 水印处理</span><span>${icon("checkCircle", 14)} 保留原始文件</span></div></div>
    <div class="process-start"><div><strong>已选 ${selected.length} 份素材</strong><span>${selected.length ? `原始体积 ${formatSize(selected.reduce((total, asset) => total + asset.sizeMB, 0))}` : "从左侧勾选要处理的内容"}</span></div><button class="btn btn-primary" data-action="start-process" ${selected.length ? "" : "disabled"}>${icon("wand", 16)} 开始模拟处理 ${selected.length ? `<span class="process-button-count">${selected.length}</span>` : ""}</button></div>
    </div></div>`
    }
  </section>`;
}

export function renderAutomation(state) {
  if (state.mode === "real") return realAutomation(state);
  return `<section class="workflow-page"><header class="page-heading"><div class="page-heading-copy"><div class="eyebrow">自动化</div><h1>自动化规则</h1><p>为常用站点设置固定流程，持续收集新的素材。</p></div><div class="page-heading-actions"><button class="btn btn-primary" data-action="new-rule">${icon("plus", 15)}新建规则</button></div></header>
    <div class="rules-flow"><div>${icon("calendar", 21)}<span><strong>定时检查</strong><small>按指定频率发现更新</small></span></div>${icon("arrowRight", 18)}<div>${icon("download", 21)}<span><strong>按规则采集</strong><small>筛选、去重并加入队列</small></span></div>${icon("arrowRight", 18)}<div>${icon("folder", 21)}<span><strong>自动归档</strong><small>按来源与分类整理</small></span></div></div>
    <div class="rules-list-heading"><h3>我的规则 <span>${state.rules.length}</span></h3><span class="muted">${state.rules.filter((rule) => rule.enabled).length} 条已开启</span></div><div class="rules-list">${state.rules.map((rule) => `<article class="rules-item"><div class="rules-icon">${icon("zap", 19)}</div><div class="rules-info"><h3>${esc(rule.name)}</h3><p><span>${icon("globe", 13)} ${esc(siteName(state, rule.siteId))}</span><span>${icon("clock", 13)} ${esc(rule.schedule)}</span><span>${icon("download", 13)} ${esc(rule.action)}</span></p></div><div class="rules-controls"><label class="rules-toggle"><input type="checkbox" data-action="toggle-rule" data-id="${esc(rule.id)}" ${rule.enabled ? "checked" : ""} aria-label="启用规则 ${esc(rule.name)}"><span></span></label><button class="btn btn-ghost" data-action="edit-rule" data-id="${esc(rule.id)}">编辑</button><button class="icon-button" data-action="delete-rule" data-id="${esc(rule.id)}" aria-label="删除规则 ${esc(rule.name)}">${icon("trash", 16)}</button></div></article>`).join("")}${state.rules.length ? "" : `<div class="empty-state">${icon("zap", 30)}<h3>把重复的工作交给规则</h3><p>创建第一条规则，选择站点和执行频率。</p><button class="btn btn-secondary" data-action="new-rule">${icon("plus", 15)} 新建规则</button></div>`}</div>
    <div class="workflow-footnote">${icon("info", 14)} 原型只保存规则配置，不会在后台访问站点或执行定时任务。</div></section>`;
}

export function renderRuleModal(state, rule = null) {
  if (state.mode === "real") return realRuleModal(state, rule);
  const schedules = ["每天 09:00", "每6小时", "每周一09:00"];
  const actions = ["下载新增素材", "扫描并等待挑选"];
  return `<div class="modal-header"><div><h2>${rule ? "编辑规则" : "新建自动化规则"}</h2><p>定义一次，按节奏收集。</p></div><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon("x", 20)}</button></div><form id="rule-form"><div class="modal-body rules-form">${rule ? `<input type="hidden" name="id" value="${esc(rule.id)}">` : ""}<label class="field"><span>规则名称</span><input class="input" name="name" required maxlength="40" placeholder="例如：每日自然风光素材" value="${esc(rule?.name || "")}"></label><label class="field"><span>来源站点</span><select class="select" name="siteId" required>${state.sites.map((site) => `<option value="${esc(site.id)}" ${rule?.siteId === site.id ? "selected" : ""}>${esc(site.name)} · ${esc(site.domain)}</option>`).join("")}</select></label><div class="rules-form-columns"><label class="field"><span>执行频率</span><select class="select" name="schedule">${schedules.map((schedule) => `<option ${rule?.schedule === schedule ? "selected" : ""}>${schedule}</option>`).join("")}</select></label><label class="field"><span>发现新内容时</span><select class="select" name="action">${actions.map((action) => `<option ${rule?.action === action ? "selected" : ""}>${action}</option>`).join("")}</select></label></div><div class="hint">${icon("info", 15)} 保存的是演示配置，暂不执行真实采集或定时任务。</div></div><div class="modal-footer"><button type="button" class="btn btn-secondary" data-action="close-modal">取消</button><button class="btn btn-primary" type="submit">${icon("check", 16)} 保存规则</button></div></form>`;
}

export function renderSettings(state, runtime = {}) {
  if (state.mode === "real") return renderRealSettings(state, runtime);
  return `<section class="workflow-page system-page"><header class="page-heading"><div class="page-heading-copy"><div class="eyebrow">演示空间</div><h1>系统</h1><p>调整任务节奏，管理当前浏览器中的演示数据。</p></div><div class="page-heading-actions"><span class="badge badge-gray">交互原型 v0.1</span></div></header><div class="settings-layout"><form id="settings-form" class="settings-main"><section class="settings-section"><div class="settings-section-title">${icon("sliders", 19)}<h3>任务与处理</h3></div><div class="settings-setting"><div><label for="setting-concurrency">同时下载的任务数</label><p>控制队列并发，避免同时启动过多任务。</p></div><input id="setting-concurrency" class="input settings-number" type="number" name="concurrency" min="1" max="5" step="1" required value="${Number(state.settings.concurrency) || 3}"></div><div class="settings-setting"><div><label for="setting-auto-process">下载完成后自动处理</label><p>新下载任务完成后，使用默认水印模板模拟处理。</p></div><label class="rules-toggle"><input type="checkbox" id="setting-auto-process" name="autoProcess" ${state.settings.autoProcess ? "checked" : ""}><span></span></label></div><div class="settings-setting"><div><label for="setting-preserve">始终保留原始文件</label><p>处理结果保存为新版本，原件不会被覆盖。</p></div><label class="rules-toggle"><input type="checkbox" id="setting-preserve" name="preserveOriginal" checked disabled><span></span></label></div>${appearanceSetting()}<div class="settings-save"><button type="submit" class="btn btn-primary">${icon("check", 16)} 保存设置</button></div></section></form><aside class="settings-context"><div class="settings-context-icon">${icon("cpu", 24)}</div><h3>轻量运行</h3><p>以 CPU 任务为基础，按需处理图片与视频水印。</p><div><span>工作区</span><strong>当前浏览器</strong></div><div><span>数据存储</span><strong>本地演示数据</strong></div><div><span>连接模式</span><strong>模拟服务</strong></div></aside></div><section class="settings-section settings-data"><div class="settings-section-title">${icon("hardDrive", 19)}<h3>演示数据</h3></div><div class="settings-setting"><div><strong>导出素材清单</strong><p>将素材名称、来源和处理状态导出为 JSON，不包含媒体文件。</p></div><button class="btn btn-secondary" data-action="export-manifest">${icon("download", 16)} 导出清单</button></div><div class="settings-setting"><div><strong>恢复初始演示</strong><p>清除当前浏览器中的原型操作记录，恢复示例素材与任务。</p></div><button class="btn btn-secondary settings-reset" data-action="reset-demo">${icon("refresh", 16)} 重置演示</button></div></section><div class="workflow-footnote">${icon("shield", 14)} 原型不收集真实密码、Cookie 或付款资料。清除浏览器数据会同时清除演示记录。</div></section>`;
}

function appearanceSetting() {
  return `<div class="settings-setting"><div><strong>外观</strong><p>深色更适合长时间看画面；选择会保存在当前浏览器。</p></div><div class="segmented" role="group" aria-label="界面外观"><button type="button" data-action="set-theme" data-value="dark">深色</button><button type="button" data-action="set-theme" data-value="light">浅色</button></div></div>`;
}
function renderRealSettings(state, runtime) {
  return `<section class="workflow-page system-page"><header class="page-heading"><div class="page-heading-copy"><div class="eyebrow">本机服务</div><h1>系统</h1><p>调整任务节奏，检查运行状态、存储与备份。</p></div><div class="page-heading-actions"><span class="badge badge-green">真实服务 v1</span></div></header>
    <div class="settings-layout"><form id="settings-form" class="settings-main"><section class="settings-section"><div class="settings-section-title">${icon("tune", 18)}<h3>偏好</h3></div><div class="settings-setting"><div><label for="setting-concurrency">同时下载</label><p>真实下载并发，保存后由本机服务调度，上限 4。</p></div><input id="setting-concurrency" class="input settings-number" type="number" name="concurrency" min="1" max="4" step="1" required value="${Number(state.settings.concurrency) || 3}"></div><div class="settings-setting"><div><strong>同时处理</strong><p>CPU 水印处理独立调度，一次执行 1 个任务。</p></div><span class="settings-fixed">1</span></div><div class="settings-setting"><div><label for="setting-auto-process">下载完成后自动处理</label><p>需要加水印时，请前往工作室选择已下载的素材。</p></div><label class="rules-toggle"><input type="checkbox" id="setting-auto-process" disabled><span></span></label></div>${appearanceSetting()}<div class="settings-save"><button type="submit" class="btn btn-primary">${icon("check", 15)}保存设置</button></div></section></form><aside class="settings-context"><div class="settings-context-icon">${icon("hardDrive", 22)}</div><h3>本机保存</h3><p>原件、成品版本与任务记录保存在本地服务；站点会话独立加密保存。</p><div><span>原始文件</span><strong>始终保留</strong></div><div><span>处理方式</span><strong>CPU</strong></div><div><span>访问权限</span><strong>按实际访问验证</strong></div></aside></div>
    ${renderOperations(runtime.data, runtime.backups || [], runtime)}
    <section class="settings-section settings-data"><div class="settings-section-title">${icon("archive", 18)}<h3>素材记录</h3></div><div class="settings-setting"><div><strong>导出素材清单</strong><p>导出名称、来源与状态；媒体文件在资料库保存。</p></div><button class="btn btn-secondary" data-action="export-manifest">${icon("download", 15)}导出清单</button></div></section></section>`;
}

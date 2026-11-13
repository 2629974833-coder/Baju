import { escapeHtml as esc, icon } from "./ui.js";
import { normalizeScanUrl } from "./model.js?v=2";

const kinds = { image: "图片", video: "视频" };
const dateFormat = new Intl.DateTimeFormat("zh-CN", {
  month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
});

export function ruleRequest(values, sites) {
  const name = String(values.get("name") || "").trim();
  if (!name) throw new Error("请输入规则名称。");
  const site = sites.find(item => item.id === values.get("siteId"));
  if (!site) throw new Error("请选择已添加的来源站点。");
  const home = site.url || `https://${site.domain}`;
  const url = normalizeScanUrl(String(values.get("url") || "").trim() || new URL(home).origin);
  if (!url) throw new Error("请填写有效的 HTTP 或 HTTPS 网址。");
  if (new URL(url).origin !== new URL(home).origin) {
    throw new Error("目标网址须与所选来源站点同源，请检查协议、域名和端口。");
  }
  const intervalMinutes = Number(values.get("intervalMinutes"));
  if (!Number.isInteger(intervalMinutes) || intervalMinutes < 15 || intervalMinutes > 10080) {
    throw new Error("检查间隔须为 15 至 10080 之间的整数分钟。");
  }
  const selected = Object.keys(kinds).filter(kind => values.getAll("kinds").includes(kind));
  if (!selected.length) throw new Error("至少选择一种素材类型。");
  return { name, siteId: site.id, url, intervalMinutes, kinds: selected,
    autoDownload: values.has("autoDownload"), enabled: values.has("enabled") };
}

// Compare with the draft opened by the user. A rule may pause for authorization
// while its dialog is open in this tab or another client edits its target.
export function changedRuleFields(original, draft) {
  const changes = {};
  for (const [key, value] of Object.entries(draft)) {
    if (key === "kinds") {
      if (original.kinds.length !== value.length || value.some(kind => !original.kinds.includes(kind))) {
        changes.kinds = value;
      }
    } else {
      const before = key === "url" ? normalizeScanUrl(original.url) : original[key];
      if (before !== value) changes[key] = value;
    }
  }
  if ("url" in changes || "siteId" in changes) {
    changes.expectedTarget = { siteId: original.siteId, url: original.url };
  }
  return changes;
}

function time(value) {
  const date = new Date(value);
  if (!value || !Number.isFinite(date.getTime())) return "—";
  return `<time datetime="${esc(value)}" title="${esc(date.toLocaleString("zh-CN"))}">${dateFormat.format(date)}</time>`;
}

function interval(minutes) {
  if (minutes % 1440 === 0) return `每 ${minutes / 1440} 天`;
  if (minutes % 60 === 0) return `每 ${minutes / 60} 小时`;
  return `每 ${Number(minutes)} 分钟`;
}

function card(rule, site) {
  const running = rule.status === "running";
  const needsAuth = rule.status === "needs-user";
  const failed = rule.status === "failed";
  let status = ["等待执行", "gray"];
  if (running) status = ["扫描中", "blue"];
  else if (needsAuth) status = ["等待授权", "amber"];
  else if (failed) status = ["执行失败", "red"];
  else if (!rule.enabled) status = ["已暂停", "gray"];
  else if (rule.status === "completed") status = ["检查完成", "green"];
  const id = esc(rule.id);
  const busy = running ? "disabled" : "";
  const types = (rule.kinds || []).map(kind => kinds[kind]).filter(Boolean).join("、");
  return `<article class="automation-card" data-rule-id="${id}">
    <div class="automation-card-top"><div class="automation-heading"><h3>${esc(rule.name)}</h3><span class="badge badge-${status[1]}">${status[0]}</span></div>
      <button class="btn btn-ghost automation-switch" data-action="toggle-rule" data-id="${id}" role="switch" aria-checked="${Boolean(rule.enabled)}" aria-label="定时执行 ${esc(rule.name)}" ${busy}><span>${rule.enabled ? "定时已开启" : "定时已暂停"}</span><i aria-hidden="true"></i></button></div>
    <div class="automation-target"><span>${icon("globe", 14)}${esc(site?.name || "未知来源")}</span><span class="automation-url" title="${esc(rule.url)}">${esc(rule.url)}</span></div>
    <p class="automation-policy">${esc(interval(rule.intervalMinutes))} · ${esc(types)} · ${rule.autoDownload ? "自动下载新增素材" : "扫描后手动挑选"}</p>
    ${needsAuth || failed ? `<div class="automation-error" role="status">${icon("alertCircle", 15)}<div>${esc(rule.error || (needsAuth ? "站点要求重新验证。" : "本次检查失败。"))}${needsAuth ? `<p>保存站点会话后，请重新开启定时执行。 <button class="text-button" data-action="auth-site" data-id="${esc(rule.siteId)}">管理授权</button></p>` : ""}</div></div>` : ""}
    <div class="automation-card-bottom"><div class="automation-timing">
      ${rule.lastRunAt ? `<span>最近执行 ${time(rule.lastRunAt)}</span>` : ""}
      ${rule.enabled && rule.nextRunAt && !running ? `<span>下次检查 ${time(rule.nextRunAt)}</span>` : ""}
      ${rule.status === "completed" ? `<span>新增 ${Number(rule.lastNewCount) || 0} 项 · 入队 ${Number(rule.lastQueuedCount) || 0} 项</span>` : ""}
    </div><div class="automation-actions">
      <button class="btn btn-secondary" data-action="run-rule" data-id="${id}" ${!rule.enabled || running ? "disabled" : ""}>${icon("play", 13)}立即执行</button>
      <button class="btn btn-ghost" data-action="edit-rule" data-id="${id}" ${busy}>编辑</button>
      <button class="icon-button" data-action="delete-rule" data-id="${id}" aria-label="删除规则 ${esc(rule.name)}" title="删除规则" ${busy}>${icon("trash", 15)}</button>
    </div></div>
  </article>`;
}

export function realAutomation(state) {
  if (state.automationAvailable === false) {
    return `<section class="workflow-page"><header class="page-heading"><div class="page-heading-copy"><h1>自动化规则</h1></div></header><div class="empty-state">${icon("refresh", 30)}<h3>请更新本机服务</h3><p>此版本的面板需要支持定时规则的后端。重启更新后的服务，再重新连接。</p><button class="btn btn-secondary" data-action="retry-service">重新连接</button></div></section>`;
  }
  const rules = [...(state.rules || [])].reverse();
  const sites = new Map(state.sites.map(site => [site.id, site]));
  return `<section class="workflow-page"><header class="page-heading"><div class="page-heading-copy"><h1>自动化规则</h1><p>定时检查来源页面，选择手动挑选或自动下载新增素材。</p></div><div class="page-heading-actions"><button class="btn btn-primary" data-action="new-rule">${icon("plus", 15)}新建规则</button></div></header>
    ${rules.length ? `<div class="rules-list-heading"><h3>我的规则 <span>${rules.length}</span></h3><span class="muted">${rules.filter(rule => rule.enabled).length} 条定时开启</span></div><div class="rules-list">${rules.map(rule => card(rule, sites.get(rule.siteId))).join("")}</div>` :
    `<div class="empty-state is-calm">${icon("calendar", 30)}<h3>还没有自动化规则</h3><p>用常看的页面创建第一条规则。服务运行时，即使关闭面板也会按时检查。</p></div>`}</section>`;
}

export function realRuleModal(state, rule) {
  const selectedKinds = rule?.kinds || ["image", "video"];
  const siteOptions = state.sites.map(site => `<option value="${esc(site.id)}" ${rule?.siteId === site.id ? "selected" : ""}>${esc(site.name)} · ${esc(site.domain)}</option>`).join("");
  return `<div class="modal-header"><div><h2>${rule ? "编辑规则" : "新建自动化规则"}</h2></div><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon("x", 20)}</button></div>
  <form id="rule-form"><div class="modal-body rules-form real-rule-form">
    <label class="field"><span>规则名称</span><input class="input" name="name" required maxlength="80" placeholder="例如：每日课程更新" value="${esc(rule?.name || "")}"></label>
    <div class="rules-form-columns">
      <label class="field"><span>来源站点</span><select class="select" name="siteId" required>${siteOptions}</select></label>
      <label class="field"><span>检查间隔（分钟）</span><input class="input" type="number" name="intervalMinutes" required min="15" max="10080" step="1" value="${Number(rule?.intervalMinutes) || 1440}"></label>
    </div>
    <label class="field"><span>目标页面</span><input class="input" type="text" inputmode="url" name="url" maxlength="8192" value="${esc(rule?.url || "")}" placeholder="留空使用站点首页"><small>每次只读取这一个页面。</small></label>
    <fieldset class="automation-kinds"><legend>素材类型</legend>${Object.entries(kinds).map(([kind, label]) => `<label><input type="checkbox" name="kinds" value="${kind}" ${selectedKinds.includes(kind) ? "checked" : ""}>${label}</label>`).join("")}</fieldset>
    <label class="automation-option"><input type="checkbox" name="autoDownload" ${rule?.autoDownload ? "checked" : ""}><span><strong>自动下载新增素材</strong><small>仅将本次首次入库的图片、识别为正片候选的视频加入下载队列。其余素材可在「发现」中挑选。</small></span></label>
    <label class="automation-option"><input type="checkbox" name="enabled" ${rule?.enabled === false ? "" : "checked"}><span><strong>开启定时执行</strong><small>首次检查将在设定间隔后执行，也可保存后立即执行。</small></span></label>
    <p class="form-error" id="rule-error" role="alert"></p>
  </div><div class="modal-footer"><button type="button" class="btn btn-secondary" data-action="close-modal">取消</button><button class="btn btn-primary" type="submit">${icon("check",16)}保存规则</button></div></form>`;
}

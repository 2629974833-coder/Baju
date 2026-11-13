import { icon, escapeHtml as esc } from "./ui.js";

export function isLataSite(site) {
  try {
    const url = new URL(site.url || `https://${site.domain}`);
    return url.protocol === 'https:' && !url.port && ['lata.casa', 'www.lata.casa'].includes(url.hostname);
  } catch { return false; }
}

const authLabels = {
  connected: { label: "已连接", className: "badge-green", icon: "checkCircle" },
  public: { label: "公开访问", className: "badge-gray", icon: "globe" },
  expired: { label: "授权已过期", className: "badge-amber", icon: "clock" },
  disconnected: { label: "未连接", className: "badge-gray", icon: "key" },
};

function authBadge(auth) {
  const status = authLabels[auth] || authLabels.disconnected;
  return `<span class="badge ${status.className}">${icon(status.icon, 13)}${status.label}</span>`;
}

function siteAvatar(site, large = false) {
  return `<span class="sites-avatar${large ? " sites-avatar-large" : ""}" aria-hidden="true">${esc(site.initials || site.name?.slice(0, 2) || "S")}</span>`;
}

function modalHeader(title, subtitle) {
  return `<div class="modal-header"><div><h2>${title}</h2><p>${subtitle}</p></div><button class="icon-button" data-action="close-modal" aria-label="关闭弹窗">${icon("x", 20)}</button></div>`;
}

function demoNote(text) {
  return `<div class="auth-demo-note">${icon("info", 17)}<span>${text}</span></div>`;
}

function missingSite() {
  return `${modalHeader("站点暂不可用", "请关闭弹窗后重新选择站点。")}<div class="modal-footer"><button class="btn btn-secondary" data-action="close-modal">关闭</button></div>`;
}

function activityTime(item) {
  const date = item.createdAt ? new Date(item.createdAt) : null;
  if (!date || Number.isNaN(date.getTime())) return item.time || "刚刚";
  return date.toLocaleString("zh-CN", {
    timeZone: "Asia/Bangkok",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function friendlyTime(value) {
  if (!value) return "尚未扫描";
  const date = /^\d{4}-\d{2}-\d{2}T/.test(value) ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return value;
  return `最近更新 ${date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false })}`;
}

export function renderSites(state) {
  const real = state.mode === "real";
  const sites = state.sites || [];
  const assets = state.assets || [];
  const connectedCount = sites.filter(
    (site) => real ? site.sessionSaved : site.auth === "connected" || site.auth === "public",
  ).length;
  const lockedCount = assets.filter((asset) => asset.locked).length;
  const activity = (state.activity || []).slice(0, 6);
  return `<section class="sites-page" aria-labelledby="sites-title">
    <header class="page-heading"><div class="page-heading-copy"><div class="eyebrow">来源站点</div><h1 id="sites-title">来源</h1><p>${sites.length} 个站点 · ${connectedCount} 个${real ? "已保存会话" : "可访问"}${lockedCount ? ` · ${lockedCount} 项待解锁` : ""}</p></div><div class="page-heading-actions"><button class="btn btn-primary" data-action="add-site">${icon("plus", 15)}添加站点</button></div></header>
    <div class="site-grid">${sites.map((site) => {
      const siteAssets = assets.filter((asset) => asset.siteId === site.id);
      const locked = siteAssets.filter((asset) => asset.locked).length;
      const local = siteAssets.filter((asset) => ["downloaded", "processed"].includes(asset.status)).length;
      const status = real ? (site.sessionSaved ? ["会话已保存", "is-jade"] : null) : [(authLabels[site.auth] || authLabels.disconnected).label, site.auth === "connected" ? "is-jade" : site.auth === "expired" ? "is-amber" : "is-gray"];
      return `<article class="site-card">
        <header>${siteAvatar(site)}<div class="site-card-name"><strong>${esc(site.name)}</strong><span>${esc(site.domain)}</span></div>${status ? `<span class="chip ${status[1]}"><i></i>${status[0]}</span>` : ""}</header>
        <dl class="site-card-facts"><div><dt>已发现</dt><dd>${siteAssets.length.toLocaleString("zh-CN")}</dd></div><div><dt>${state.publicMode === true ? "已下载" : "已在本机"}</dt><dd>${local.toLocaleString("zh-CN")}</dd></div><div><dt>${real ? "站点访问" : "权限"}</dt><dd>${esc(real ? (site.sessionSaved ? "使用会话" : "访客访问") : site.access || "未配置")}</dd></div></dl>
        ${locked ? `<button class="site-card-note" data-action="unlock-site" data-id="${esc(site.id)}">${icon("lock", 13)}${locked} 项需要权限${icon("chevronRight", 13)}</button>` : `<p class="site-card-note is-quiet">${esc(friendlyTime(site.updated))}</p>`}
        <footer><button class="btn btn-primary" data-action="scan-site" data-id="${esc(site.id)}" aria-label="扫描 ${esc(site.name)}">${icon("search", 15)}扫描站点</button><button class="btn btn-secondary" data-action="auth-site" data-id="${esc(site.id)}">${real ? "站点授权" : "授权管理"}</button></footer>
      </article>`;
    }).join("")}<button class="site-card site-card-add" data-action="add-site">${icon("plus", 22)}<strong>添加站点</strong><span>保存常用网址，随时发起扫描</span></button></div>
    <div class="sites-bottom-layout">
      <section class="sites-activity"><div class="sites-section-heading"><h2>最近活动</h2><span class="sites-live-dot">${real ? state.publicMode === true ? "你的临时空间" : "本地工作空间" : "本次演示"}</span></div>
        ${activity.length ? `<ol>${activity.map((item) => `<li><span class="sites-activity-mark">${icon("check", 12)}</span><span>${esc(item.message || item.title || "状态已更新")}</span><time>${esc(activityTime(item))}</time></li>`).join("")}</ol>` : '<p class="sites-no-activity">授权、解锁和任务操作会记录在这里。</p>'}
      </section>
      <aside class="sites-help"><span class="sites-help-icon">${icon("key", 20)}</span><h3>何时需要站点授权？</h3><p>${real ? "公开页面可直接扫描。要读取会员或私有内容，请在对应站点的「站点授权」中打开登录窗口，完成验证后保存会话。" : "公开来源可直接体验扫描。需要模拟站点权限时，选择对应来源的「授权管理」。"}</p></aside>
    </div>
  </section>`;
}

export function renderAuthModal(state, siteId, dialog = {}) {
  const site = state.sites.find((item) => item.id === siteId);
  if (!site) return missingSite();
  if (state.mode === "real") return renderRealAuthModal(site, state.publicMode === true ? {...dialog, localInfo: null, publicMode:true} : dialog);
  const connected = site.auth === "connected";
  const isPublic = site.auth === "public";
  const hasLockedAssets = state.assets.some(
    (asset) => asset.siteId === siteId && asset.locked,
  );
  const canConnect = !connected && (!isPublic || hasLockedAssets);
  const statusCopy = connected
    ? "演示会话已连接，可继续扫描和解锁素材。"
    : isPublic
      ? hasLockedAssets
        ? "公开素材可直接浏览，锁定内容需要先连接演示账号。"
        : "此来源的公开素材无需登录即可浏览。"
      : site.auth === "expired"
        ? "演示会话已过期，重新授权后可继续任务。"
        : "连接演示会话，体验登录后的访问流程。";

  return `${modalHeader("站点授权", "在这里查看和管理站点访问状态。")}
    <div class="modal-body auth-body">
      <div class="auth-site-summary">${siteAvatar(site, true)}<div><h3>${esc(site.name)}</h3><span>${esc(site.domain)}</span></div>${authBadge(site.auth)}</div>
      <p class="auth-status-copy">${statusCopy}</p>
      <dl class="auth-details">
        <div><dt>${icon("user", 15)}当前会话</dt><dd>${connected ? "演示账号 · 已授权" : isPublic ? "访客访问" : "等待授权"}</dd></div>
        <div><dt>${icon("key", 15)}访问权限</dt><dd>${esc(site.access || "未配置")}</dd></div>
        <div><dt>${icon("clock", 15)}会话有效期</dt><dd>${connected ? "30 天（演示）" : isPublic ? "无需授权" : "未生效"}</dd></div>
      </dl>
      ${isPublic && !hasLockedAssets ? "" : `<div class="auth-steps"><div><span>01</span><strong>站点登录</strong><small>由你完成身份验证</small></div>${icon("chevronRight", 15)}<div><span>02</span><strong>连接会话</strong><small>保存站点授权状态</small></div>${icon("chevronRight", 15)}<div><span>03</span><strong>同步权限</strong><small>查看可访问的素材</small></div></div>`}
      ${demoNote("当前为交互原型，不会连接真实账号，也无需输入密码、Cookie 或支付信息。")}
    </div>
    <div class="modal-footer auth-footer">
      ${connected ? `<button class="btn btn-ghost auth-disconnect" data-action="disconnect-site" data-id="${esc(site.id)}">${icon("logOut", 16)}断开演示会话</button>` : "<span></span>"}
      <div><button class="btn btn-secondary" data-action="close-modal">关闭</button>${canConnect ? `<button class="btn btn-primary" data-action="connect-site" data-id="${esc(site.id)}">${icon("key", 16)}${isPublic ? "模拟登录解锁" : "模拟授权连接"}</button>` : ""}</div>
    </div>`;
}

export function renderUnlockModal(state, assetIds) {
  if (state.mode === "real") return renderRealUnlockModal(state, assetIds);
  const selected = new Set(assetIds || []);
  const assets = state.assets.filter(
    (asset) => selected.has(asset.id) && asset.locked,
  );
  const total = assets.reduce(
    (sum, asset) => sum + (Number(asset.price) || 0),
    0,
  );
  const needsAuth = assets
    .map((asset) => state.sites.find((site) => site.id === asset.siteId))
    .find((site) => site && site.auth !== "connected");
  const ids = assets.map((asset) => asset.id).join(",");

  return `${modalHeader("解锁所选素材", "先确认访问权限，再将内容加入下载队列。")}
    <div class="modal-body unlock-body">
      ${
        assets.length
          ? `<div class="unlock-list">${assets
              .map((asset) => {
                const site = state.sites.find(
                  (item) => item.id === asset.siteId,
                );
                return `<div class="unlock-item"><img src="${esc(asset.thumbnail)}" alt="" loading="lazy"><div><strong>${esc(asset.title)}</strong><span>${esc(site?.name || "未知站点")} · ${asset.kind === "video" ? "视频" : "图片"}</span></div><span class="unlock-price">${Number(asset.price) || 0}<small>演示金币</small></span></div>`;
              })
              .join("")}</div>
      <div class="unlock-total"><span>共 ${assets.length} 项素材</span><div>合计 <strong>${total}</strong><span>演示金币</span></div></div>`
          : `<div class="empty-state">${icon("unlock", 28)}<h3>素材已解锁</h3><p>可以返回素材库继续下载。</p></div>`
      }
      ${needsAuth ? `<div class="unlock-auth-notice">${icon("key", 17)}<span>请先连接 ${esc(needsAuth.name)} 的演示会话。</span></div>` : ""}
      ${demoNote("此操作仅模拟付费解锁，不会扣款或向站点发起真实交易。真实解锁需由你在站点确认。")}
    </div>
    <div class="modal-footer"><button class="btn btn-secondary" data-action="close-modal">取消</button>${needsAuth ? `<button class="btn btn-primary" data-action="auth-site" data-id="${esc(needsAuth.id)}">${icon("key", 16)}先连接站点</button>` : `<button class="btn btn-primary" data-action="confirm-unlock" data-ids="${esc(ids)}" ${assets.length ? "" : "disabled"}>${icon("unlock", 16)}确认模拟解锁</button>`}</div>`;
}

export function renderAddSiteModal(state = {}) {
  return `${modalHeader("添加素材站点", "接入新的来源，为它建立独立的素材集合。")}
    <form id="add-site-form">
      <div class="modal-body auth-body">
        <label class="field" for="new-site-name"><span>站点名称</span><input class="input" id="new-site-name" name="name" placeholder="例如：我的素材库" maxlength="40" required autocomplete="off"></label>
        <label class="field" for="new-site-url"><span>站点地址</span><div class="sites-url-field">${icon("link", 17)}<input class="input" id="new-site-url" name="url" placeholder="https://example.com" inputmode="url" required autocomplete="off" spellcheck="false"></div><small class="hint">支持完整 URL 或域名；扫描范围可在创建任务时设置。</small></label>
        ${demoNote(state.mode === "real" ? "添加后可直接扫描公开页面。需要访问受限内容时，可在该站点保存授权会话。" : "添加后将生成一个演示站点。原型不会访问该地址或抓取真实内容。")}
      </div>
      <div class="modal-footer"><button type="button" class="btn btn-secondary" data-action="close-modal">取消</button><button type="submit" class="btn btn-primary">${icon("plus", 16)}添加站点</button></div>
    </form>`;
}

function realSessionBadge(site) {
  return `<span class="badge ${site.sessionSaved ? "badge-green" : "badge-gray"}">${icon(site.sessionSaved ? "checkCircle" : "key", 13)}${site.sessionSaved ? "会话已保存" : "未连接"}</span>`;
}
function localBrowserControls(site, dialog) {
  const info = dialog.localInfo;
  const error = dialog.localError || dialog.localReadError;
  const systemBrowser = info?.mode === "system-browser";
  const googleLogin = isLataSite(site);
  const canImport = info?.canImportAccount === true;
  const notice = error || dialog.localNotice || (!canImport && (!systemBrowser || !info.available) ? info?.message : "") || "";
  if (!canImport && !info?.available && !info?.active && !notice) return "";
  const busy = dialog.localBusy ? "disabled" : "";
  const id = esc(site.id);
  let instruction = info?.active
    ? "在打开的浏览器中手动完成站点验证并登录。确认站点显示你的账号后，回到这里保存会话。"
    : "需要手动通过 Cloudflare 等人机验证时，可以在运行拾帧的电脑上打开独立浏览器。";
  if (systemBrowser) {
    instruction = info.active
      ? info.message || "在专用浏览器中完成验证和登录，看到自己的账号后回到这里保存会话。"
      : `在本机 Chrome 或 Edge 中打开 ${site.name}，由你完成验证和登录。`;
    if (googleLogin && !info.active) {
      instruction = "在本机 Chrome 或 Edge 中点击 Continue with Google，完成登录并返回 Lata，再回到这里保存会话。";
    }
  }
  if (canImport && !info.active) {
    instruction = "用已登录春楼的日常浏览器打开下面的授权链接。如果面板开在其他浏览器，可复制链接到日常浏览器中打开。";
  }
  let accountLink = "";
  if (canImport) {
    accountLink = info.active || dialog.localBusy
      ? '<button type="button" class="btn btn-secondary" disabled>使用已登录的浏览器</button>'
      : `<a class="btn btn-primary" href="/chunlou-connect.html?site=${esc(encodeURIComponent(site.id))}" target="_blank" rel="noopener noreferrer">使用已登录的浏览器</a>`;
  }
  const actions = info?.active
    ? `<button class="btn btn-primary" data-action="save-local-auth" data-id="${id}" ${busy}>${dialog.localBusy === "save" ? "正在保存…" : "保存本机浏览器会话"}</button>
       <button class="btn btn-ghost" data-action="cancel-local-auth" data-id="${id}" ${busy}>${dialog.localBusy === "cancel" ? "正在取消…" : "取消本机授权"}</button>`
    : info?.available
      ? `<button class="btn ${systemBrowser && !canImport ? "btn-primary" : "btn-secondary"}" data-action="start-local-auth" data-id="${id}" ${busy}>${dialog.localBusy === "start" ? "正在打开浏览器…" : "使用本机浏览器授权"}</button>`
      : "";
  return `<section class="auth-local-browser"><strong>本机浏览器</strong>
    ${canImport || info?.available || info?.active ? `<p>${esc(instruction)}</p>` : ""}
    ${accountLink || actions ? `<div class="auth-local-actions">${accountLink}${actions}</div>` : ""}
    ${canImport && info.active ? "<p>要改用已登录的日常浏览器，请先保存或取消当前专用窗口的授权。</p>" : ""}
    ${systemBrowser && info.available && !googleLogin ? `<details class="auth-local-help"><summary>Cloudflare 仍然验证失败？</summary>
      <p>保持当前网络，用平时的浏览器访问<a href="https://chunlou.tv/auth?r=/feed" target="_blank" rel="noopener noreferrer">春楼登录页</a>。如果拾帧开在内置浏览器，可复制这个链接。</p>
      <p>如果两个窗口都失败，在同一浏览器运行 <a href="https://debug.challenges.cloudflare.com/" target="_blank" rel="noopener noreferrer">Cloudflare 兼容性检测</a>，也可用手机热点做一次对照。</p>
      <p>若显示 <code>110200</code>，请联系春楼检查域名授权。若显示 <code>200500</code>，请检查网络或扩展是否拦截了验证组件。<a href="https://developers.cloudflare.com/turnstile/troubleshooting/client-side-errors/error-codes/" target="_blank" rel="noopener noreferrer">查看官方错误码表</a></p>
    </details>` : ""}
    ${dialog.localReadError ? `<button class="btn btn-ghost" data-action="check-local-auth" data-id="${id}">重新检查</button>` : ""}
    ${notice ? `<p role="${error ? "alert" : "status"}" class="${error ? "form-error" : "auth-local-status"}">${esc(notice)}</p>` : ""}
  </section>`;
}
function renderRealAuthModal(site, dialog) {
  const engaged = dialog.localBusy || dialog.localInfo?.active;
  const preferSystem = dialog.localInfo?.canImportAccount === true || (dialog.localInfo?.available && dialog.localInfo.mode === "system-browser");
  const instructions = site.id === "chunlou" || site.domain === "chunlou.tv"
    ? "若春楼先要求确认年龄，请完成后继续登录账号。"
    : "请在站点内登录并完成验证，再回到拾帧保存会话。";
  return `${modalHeader("站点授权", `在 ${esc(site.name)} 的窗口登录后，可保存该站点的会话。`)}
    <div class="modal-body auth-body"><div class="auth-site-summary">${siteAvatar(site, true)}<div><h3>${esc(site.name)}</h3><span>${esc(site.domain)}</span></div>${realSessionBadge(site)}</div>
    <p class="auth-status-copy">${site.sessionSaved ? "会话已保存，权限待验证。可重新拾取目标链接检查访问权限。" : instructions}</p>
    ${dialog.publicMode ? '<p class="hint">会话仅供当前临时空间使用。部分站点的人机验证或登录方式可能无法在此完成。</p>' : localBrowserControls(site, dialog)}</div>
    <div class="modal-footer auth-footer"><button class="btn btn-ghost auth-disconnect" data-action="disconnect-site" data-id="${esc(site.id)}" ${engaged ? "disabled" : ""}>${icon("logOut", 16)}清除会话</button><div><button class="btn btn-secondary" data-action="scan-site" data-id="${esc(site.id)}" ${dialog.localBusy ? "disabled" : ""}>扫描站点</button><button class="btn ${preferSystem ? "btn-secondary" : "btn-primary"}" data-action="connect-site" data-id="${esc(site.id)}" ${engaged ? "disabled" : ""}>${icon("key", 16)}${preferSystem ? "面板内登录" : "打开登录窗口"}</button></div></div>`;
}
function renderRealUnlockModal(state, assetIds) {
  const ids = new Set(assetIds || []);
  const siteIds = new Set(state.assets.filter((asset) => ids.has(asset.id)).map((asset) => asset.siteId));
  const sites = state.sites.filter((site) => siteIds.has(site.id));
  return `${modalHeader("管理内容访问权限", "请在来源站点确认登录或付费权限。")}
    <div class="modal-body"><p class="auth-status-copy">本应用不会自动购买或模拟解锁。可打开站点浏览器，自行操作后保存会话，再重新扫描页面。</p>${sites.map((site) => `<div class="unlock-item">${siteAvatar(site)}<div><strong>${esc(site.name)}</strong><span>${esc(site.domain)}</span></div><button class="btn btn-secondary" data-action="connect-site" data-id="${esc(site.id)}">打开站点</button></div>`).join("")}</div><div class="modal-footer"><button class="btn btn-secondary" data-action="close-modal">关闭</button></div>`;
}

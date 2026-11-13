import { escapeHtml as esc, formatSize, icon } from './ui.js';

const bytes = value => Number.isFinite(Number(value)) && value !== null ? formatSize(Number(value) / 1024 ** 2) : '未知';
const count = value => Math.max(0, Math.floor(Number(value) || 0));
const types = { automation: '自动化', download: '下载', processing: '处理', process: '处理', scans: '扫描', scan: '扫描' };

function uptime(seconds) {
  if (!Number.isFinite(Number(seconds))) return '未知';
  const minutes = Math.floor(Number(seconds) / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟` : `${minutes} 分钟`;
}

function queueTable(queues = {}) {
  return `<div class="operations-table-wrap"><table class="table operations-queue"><thead><tr><th>任务</th><th>运行</th><th>排队</th><th>暂停</th><th>失败</th><th>需权限</th><th>完成</th></tr></thead><tbody>${['download', 'processing', 'scans'].map(type => `<tr><th>${types[type]}</th>${['running', 'queued', 'paused', 'failed', 'needsUser', 'completed'].map(key => `<td>${count(queues[type]?.[key])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

function backupsSection(operations, backups, busy) {
  const active = Object.values(operations?.queues || {}).some(queue => count(queue.running) + count(queue.queued) > 0);
  const unavailable = !operations || active || busy;
  return `<section class="settings-section operations-backups"><div class="operations-section-heading"><div><h3>备份与恢复</h3><p>包含数据库、媒体原件、处理版本及已保存会话。</p></div><button class="btn btn-secondary" data-action="backup-create" ${unavailable ? 'disabled' : ''}>${icon('hardDrive', 15)}${busy ? '正在操作…' : '创建完整备份'}</button></div>${active ? '<p class="operations-notice">请先暂停排队及运行中的下载、处理，并取消活动扫描，待任务停止后再备份。</p>' : ''}<div class="operations-backup-list">${backups.length ? backups.map(backup => {
    const verified = backup.verified === true && backup.verification === 'verified';
    return `<article class="operations-backup"><div><strong>${esc(backup.createdAt || backup.id)}</strong><p>${count(backup.fileCount)} 个文件 · ${bytes(backup.totalBytes)} · ${esc(backup.path || '本机备份目录')}</p></div><span class="badge ${verified ? 'badge-green' : 'badge-gray'}">${verified ? '本次校验通过' : '尚未复查'}</span><button class="btn btn-ghost" data-action="backup-verify" data-id="${esc(backup.id)}" ${busy ? 'disabled' : ''}>校验备份</button></article>`;
  }).join('') : '<p class="operations-empty">尚无备份记录。创建后可在这里再次校验完整性。</p>'}</div><p class="operations-backup-help">备份包含已保存的来源站点会话，请按私有数据保管。恢复需先停止服务，通过维护命令恢复到新空目录，再验证启动；面板不会覆盖当前工作区。</p></section>`;
}

export function renderOperations(operations, backups = [], { loading = false, error = '', busy = false } = {}) {
  const header = `<div class="operations-heading"><div><h2>运行维护</h2><p>查看本机服务、任务调度和存储状态。</p></div><button class="btn btn-secondary" data-action="refresh-operations" ${loading ? 'disabled' : ''}>${icon('refresh', 14)}刷新状态</button></div>`;
  if (!operations) return `<section class="operations-panel">${header}<div class="operations-unavailable" role="status">${esc(error || (loading ? '正在读取服务状态…' : '尚未读取运行状态，请刷新。'))}</div></section>`;
  const disk = operations.disk || {};
  const usage = Number(disk.totalBytes) > 0 ? Math.max(0, Math.min(100, Number(disk.usedBytes) / Number(disk.totalBytes) * 100)) : 0;
  const dependencyNames = { ffmpeg: 'FFmpeg', ffprobe: 'FFprobe', chromium: 'Chromium', pillow: 'Pillow' };
  return `<section class="operations-panel">${header}${error ? `<p class="operations-notice" role="alert">${esc(error)} 以下保留上次读取结果，请刷新后再判断状态。</p>` : ''}<div class="operations-status-line"><span class="badge ${operations.ready === true ? 'badge-green' : 'badge-amber'}">${operations.ready === true ? '运行正常' : '需要关注'}</span><span>已运行 ${uptime(operations.uptimeSeconds)}</span><span>数据库：${operations.database?.available === true ? '可用' : '不可用'}</span></div><div class="operations-columns"><section class="operations-storage"><h3>磁盘空间</h3><div class="operations-disk-track" role="meter" aria-label="磁盘已用比例" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(usage)}"><i style="width:${usage}%"></i></div><dl><div><dt>磁盘剩余</dt><dd>${bytes(disk.freeBytes)}</dd></div><div><dt>保护预留</dt><dd>${bytes(disk.minFreeBytes)}</dd></div><div><dt>可供任务使用</dt><dd>${bytes(disk.availableBytes)}</dd></div></dl>${disk.healthy === false ? '<p class="operations-notice">磁盘容量不足或低于预留线，新的写入任务将暂停等待释放空间。</p>' : '<p>任务写入前和执行中检查可用空间，保留容量用于服务运行。</p>'}</section><section class="operations-runtime"><h3>调度与依赖</h3><ul>${['download', 'processing', 'automation'].filter(type => type !== 'automation' || operations.workers?.automation).map(type => {
    const worker = operations.workers?.[type];
    const healthy = worker?.healthy === true;
    return `<li><span>${types[type]}调度</span><strong class="${healthy ? '' : 'operations-warning'}">${healthy ? '心跳正常' : '心跳异常'}</strong><small>${typeof worker?.heartbeatAgeSeconds === 'number' && Number.isFinite(worker.heartbeatAgeSeconds) ? `${Math.round(worker.heartbeatAgeSeconds)} 秒前` : '尚未收到'}</small></li>`;
  }).join('')}${Object.entries(dependencyNames).map(([key, label]) => `<li><span>${label}</span><strong class="${operations.dependencies?.[key]?.available === true ? '' : 'operations-warning'}">${operations.dependencies?.[key]?.available === true ? '可用' : '未就绪'}</strong></li>`).join('')}</ul></section></div><section class="operations-queues"><h3>任务概况</h3>${queueTable(operations.queues)}</section>${operations.recentFailures?.length ? `<section class="operations-failures"><h3>最近需要关注的任务</h3><ul>${operations.recentFailures.slice(0, 10).map(item => `<li><span>${types[item.type] || '任务'} · ${esc(item.id)}</span><span>${esc(item.code || item.status || '失败')}</span><time>${esc(item.at || '')}</time></li>`).join('')}</ul></section>` : ''}${backupsSection(operations, backups, busy)}</section>`;
}

/** API errors must remain visible: only capabilities 404 is a static preview. */
export function createApiClient(fetcher = (...args) => fetch(...args)) {
  let automationAvailable = false;
  let intakeAvailable = false;
  let capabilities = {};
  async function request(path, { method = 'GET', body, blob = false, allowMissing = false, signal } = {}) {
    let response;
    try {
      response = await fetcher(path, {
        method,
        headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: 'no-store',
        signal,
      });
    } catch (error) {
      if (error.name === 'AbortError') throw error;
      throw new Error('无法连接下载服务，请稍后重试。');
    }
    if (allowMissing && response.status === 404) return null;
    if (!response.ok) {
      let detail;
      try { detail = (await response.json()).detail; } catch { /* Error pages may be HTML. */ }
      throw new Error(typeof detail === 'string' ? detail : `服务请求失败（HTTP ${response.status}），请重试。`);
    }
    if (blob) return response.blob();
    if (response.status === 204) return null;
    try { return await response.json(); }
    catch { throw new Error('服务响应格式不正确，请稍后重试。'); }
  }
  return {
    request,
    async mode() {
      const result = await request('/api/capabilities', { allowMissing: true });
      capabilities = result || {};
      automationAvailable = result?.automation === true;
      intakeAvailable = ["site", "series", "single"].every(scope => result?.intakeScopes?.includes(scope));
      if (result === null) return 'demo';
      if (result.mode !== 'real' || result.version !== 1) throw new Error('服务协议不兼容，请更新服务后重试。');
      return 'real';
    },
    async state() {
      const value = await request('/api/state');
      const publicMode = capabilities.publicMode === true || value?.publicMode === true;
      const state = normalizeRealState({...value, publicMode, retentionHours:value?.retentionHours ?? capabilities.retentionHours ?? 24});
      return {...state, automationAvailable:!publicMode && automationAvailable, intakeAvailable};
    },
  };
}

export function normalizeRealState(value) {
  if (!value || value.mode !== 'real' || value.version !== 1 || !Array.isArray(value.assets) || !Array.isArray(value.sites)) {
    throw new Error('服务返回的工作区状态无效，请重试。');
  }
  return {
    ...value,
    assets: value.assets.map((asset) => ({
      tags: [], versions: [], width: 0, height: 0, sizeMB: 0, category: '未分类', duration: '—', date: '',
      ...asset,
      sizeMB: Number(asset.sizeBytes) > 0 ? Number(asset.sizeBytes) / 1024 ** 2 : Number(asset.sizeMB) || 0,
      tags: Array.isArray(asset.tags) ? asset.tags : [],
      versions: Array.isArray(asset.versions) ? asset.versions : [],
      thumbnail: asset.thumbnail || 'assets/favicon.svg',
    })),
    jobs: Array.isArray(value.jobs) ? value.jobs : [],
    processingJobs: Array.isArray(value.processingJobs) ? value.processingJobs : [],
    scans: value.scans || [], rules: Array.isArray(value.rules) ? value.rules : [], activity: value.activity || [],
    settings: { concurrency: 3, preserveOriginal: true, ...value.settings, autoProcess: false },
  };
}

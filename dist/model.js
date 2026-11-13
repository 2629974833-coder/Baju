// Local prototype state. These transitions simulate jobs; they do not fetch media
// or unlock remote content. Original asset metadata is retained for every output.
const ACTIVE = new Set(["running", "queued", "paused"]);
const AUTH = new Set(["connected", "expired", "public", "disconnected"]);
const DEFAULT_PRESET = "品牌水印 · 右下角";
let sequence = 0;
const uid = (prefix) => `${prefix}-${Date.now().toString(36)}-${++sequence}`;
const now = () => new Date().toISOString();
const copy = (state) => structuredClone(state);
const getAsset = (state, id) => state.assets.find((item) => item.id === id);
const getSite = (state, id) => state.sites.find((item) => item.id === id);

function record(state, type, message) {
  state.activity.unshift({ id: uid("event"), type, message, createdAt: now() });
  state.activity = state.activity.slice(0, 100);
}

function downloadError(state, asset) {
  if (!asset) return "素材不存在";
  if (asset.locked) return "此素材尚未解锁";
  const site = getSite(state, asset.siteId);
  if (!site) return "来源站点不存在";
  if (site.auth === "expired") return "站点登录已过期，请重新连接";
  if (site.auth !== "connected" && site.auth !== "public")
    return "请先连接来源站点";
  return null;
}

function makeJob(assetId, type, preset) {
  const job = {
    id: uid("job"),
    assetId,
    type,
    status: "queued",
    progress: 0,
    createdAt: now(),
  };
  if (preset !== undefined) job.preset = structuredClone(preset);
  return job;
}

function outputVersion(asset, preset) {
  const label =
    typeof preset === "string"
      ? preset
      : preset?.name || preset?.label || DEFAULT_PRESET;
  return {
    id: uid("version"),
    name: label,
    label,
    preset: structuredClone(preset ?? DEFAULT_PRESET),
    createdAt: now(),
    thumbnail: asset.thumbnail,
    width: asset.width,
    height: asset.height,
    sizeMB: asset.sizeMB,
  };
}

export function createInitialState() {
  const rows = [
    [
      "海岸线的慢镜头",
      "video",
      "自然风光",
      "s1",
      "coast",
      "04:32",
      3840,
      2160,
      286,
      "discovered",
    ],
    [
      "雪山之上 · 日出时分",
      "video",
      "自然风光",
      "s1",
      "mountain",
      "03:18",
      3840,
      2160,
      214,
      "discovered",
      true,
      12,
    ],
    [
      "城市清晨的第一束光",
      "video",
      "城市建筑",
      "s1",
      "city",
      "02:46",
      1920,
      1080,
      128,
      "discovered",
      true,
      8,
    ],
    [
      "沙丘与风的轨迹",
      "image",
      "自然风光",
      "s1",
      "desert",
      "",
      5000,
      3333,
      8.4,
      "discovered",
      true,
      6,
    ],
    [
      "森林中的绿色呼吸",
      "image",
      "自然风光",
      "s2",
      "forest",
      "",
      4000,
      6000,
      12.8,
      "downloaded",
    ],
    [
      "海面上的蓝色褶皱",
      "video",
      "自然风光",
      "s2",
      "ocean",
      "01:54",
      3840,
      2160,
      156,
      "downloaded",
    ],
    [
      "光与建筑的对话",
      "image",
      "城市建筑",
      "s2",
      "architecture",
      "",
      4000,
      3000,
      7.2,
      "downloaded",
    ],
    [
      "房间里的午后",
      "image",
      "生活方式",
      "s1",
      "interior",
      "",
      3600,
      2400,
      5.6,
      "downloaded",
    ],
    [
      "湖泊与远山",
      "image",
      "自然风光",
      "s2",
      "lake",
      "",
      6000,
      4000,
      16.2,
      "processed",
    ],
    [
      "奔向旷野的公路",
      "video",
      "旅行探索",
      "s2",
      "road",
      "05:12",
      3840,
      2160,
      342,
      "processed",
    ],
    [
      "金色麦田的季节",
      "video",
      "生活方式",
      "s2",
      "field",
      "02:08",
      1920,
      1080,
      96,
      "discovered",
    ],
    [
      "冬日雪原 · 航拍",
      "video",
      "自然风光",
      "s3",
      "snow",
      "03:42",
      3840,
      2160,
      268,
      "discovered",
    ],
    [
      "峡湾的静谧时刻",
      "image",
      "自然风光",
      "s2",
      "coast",
      "",
      4200,
      2800,
      9.1,
      "discovered",
    ],
    [
      "城市几何",
      "image",
      "城市建筑",
      "s1",
      "architecture",
      "",
      3000,
      4000,
      8.6,
      "discovered",
    ],
    [
      "沿着湖岸散步",
      "video",
      "旅行探索",
      "s2",
      "lake",
      "01:36",
      1920,
      1080,
      72,
      "discovered",
    ],
    [
      "山间公路的尽头",
      "video",
      "旅行探索",
      "s3",
      "road",
      "04:06",
      3840,
      2160,
      308,
      "discovered",
    ],
  ];
  const assets = rows.map((row, index) => {
    const [
      title,
      kind,
      category,
      siteId,
      image,
      duration,
      width,
      height,
      sizeMB,
      status,
      locked = false,
      price = 0,
    ] = row;
    const asset = {
      id: `a${index + 1}`,
      title,
      kind,
      category,
      siteId,
      thumbnail: `assets/${image}.jpg`,
      duration,
      width,
      height,
      sizeMB,
      date: "2026-11-03",
      status,
      locked,
      price,
      tags: [category, width >= 3840 ? "4K" : "精选"],
      favorite: [0, 4, 8].includes(index),
      versions: [],
    };
    if (status === "processed")
      asset.versions.push({
        id: `v${index + 1}`,
        name: DEFAULT_PRESET,
        label: DEFAULT_PRESET,
        preset: DEFAULT_PRESET,
        createdAt: "2026-11-03T02:30:00Z",
        thumbnail: asset.thumbnail,
        width,
        height,
        sizeMB,
      });
    return asset;
  });
  const sites = [
    {
      id: "s1",
      name: "春楼 CHUNLOU",
      domain: "chunlou.tv",
      initials: "CL",
      auth: "connected",
      access: "VIP 会员",
      updated: "刚刚同步",
    },
    {
      id: "s2",
      name: "Pexels",
      domain: "pexels.com",
      initials: "P",
      auth: "public",
      access: "公开访问",
      updated: "12 分钟前",
    },
    {
      id: "s3",
      name: "Vimeo",
      domain: "vimeo.com",
      initials: "V",
      auth: "expired",
      access: "登录已过期",
      updated: "1 小时前",
    },
  ].map((site) => ({
    ...site,
    count: assets.filter((item) => item.siteId === site.id).length,
  }));
  return {
    version: 1,
    assets,
    sites,
    jobs: [
      {
        id: "j1",
        assetId: "a1",
        type: "download",
        status: "running",
        progress: 42,
        createdAt: "2026-11-03T03:00:00Z",
      },
      {
        id: "j2",
        assetId: "a11",
        type: "download",
        status: "paused",
        progress: 28,
        createdAt: "2026-11-03T02:59:00Z",
      },
      {
        id: "j3",
        assetId: "a12",
        type: "download",
        status: "failed",
        progress: 0,
        error: "站点登录已过期，请重新连接",
        createdAt: "2026-11-03T02:58:00Z",
      },
      {
        id: "j4",
        assetId: "a5",
        type: "download",
        status: "completed",
        progress: 100,
        createdAt: "2026-11-03T02:40:00Z",
      },
    ],
    rules: [
      {
        id: "r1",
        name: "每日同步新素材",
        siteId: "s1",
        schedule: "每天 09:00",
        enabled: true,
        action: "扫描并等待挑选",
      },
      {
        id: "r2",
        name: "自然风光灵感收集",
        siteId: "s2",
        schedule: "每周一09:00",
        enabled: true,
        action: "下载新增素材",
      },
      {
        id: "r3",
        name: "Vimeo 关注更新",
        siteId: "s3",
        schedule: "每6小时",
        enabled: false,
        action: "扫描并等待挑选",
      },
    ],
    settings: { concurrency: 3, autoProcess: false, preserveOriginal: true },
    activity: [
      {
        id: "e1",
        type: "scan",
        message: "演示扫描已完成，发现 16 个素材",
        createdAt: "2026-11-03T03:00:00Z",
      },
      {
        id: "e2",
        type: "process",
        message: "2 个素材已生成处理版本，原文件已保留",
        createdAt: "2026-11-03T02:30:00Z",
      },
    ],
  };
}

export function enqueueDownloads(state, ids) {
  const next = copy(state);
  const result = { state: next, added: 0, blocked: 0, duplicates: 0 };
  for (const id of ids) {
    const asset = getAsset(next, id);
    if (
      asset &&
      (asset.status !== "discovered" ||
        next.jobs.some(
          (job) =>
            job.assetId === id &&
            job.type === "download" &&
            job.status !== "failed",
        ))
    ) {
      result.duplicates++;
    } else if (downloadError(next, asset)) {
      result.blocked++;
    } else {
      next.jobs.push(makeJob(id, "download"));
      result.added++;
    }
  }
  if (result.added)
    record(next, "download", `${result.added} 个素材已加入下载队列`);
  return result;
}

export function startProcessing(state, ids, preset = DEFAULT_PRESET) {
  const next = copy(state);
  const result = { state: next, added: 0, blocked: 0, duplicates: 0 };
  for (const id of ids) {
    const asset = getAsset(next, id);
    if (
      next.jobs.some(
        (job) =>
          job.assetId === id &&
          job.type === "process" &&
          ACTIVE.has(job.status),
      )
    ) {
      result.duplicates++;
    } else if (!asset || !["downloaded", "processed"].includes(asset.status)) {
      result.blocked++;
    } else {
      next.jobs.push(makeJob(id, "process", preset));
      result.added++;
    }
  }
  if (result.added)
    record(next, "process", `${result.added} 个素材已加入处理队列`);
  return result;
}

export function unlockAssets(state, ids) {
  const next = copy(state);
  let unlocked = 0;
  for (const id of new Set(ids)) {
    const asset = getAsset(next, id);
    if (asset?.locked && getSite(next, asset.siteId)?.auth === "connected") {
      asset.locked = false;
      unlocked++;
    }
  }
  if (unlocked)
    record(next, "unlock", `已模拟解锁 ${unlocked} 个素材，未产生真实扣费`);
  return next;
}

export function setSiteAuth(state, siteId, auth) {
  const next = copy(state);
  const site = getSite(next, siteId);
  if (site && AUTH.has(auth) && site.auth !== auth) {
    site.auth = auth;
    site.updated = "刚刚更新";
    site.access = {
      connected: site.id === "s1" ? "VIP 会员" : "已授权",
      expired: "登录已过期",
      public: "公开访问",
      disconnected: "未连接",
    }[auth];
    record(next, "site", `${site.name} · ${site.access}`);
  }
  return next;
}

export function transitionJob(state, id, action) {
  const next = copy(state);
  const job = next.jobs.find((item) => item.id === id);
  if (!job) return next;
  if (action === "cancel" && job.status !== "completed") {
    next.jobs = next.jobs.filter((item) => item.id !== id);
    record(next, "job", "任务已取消");
  } else if (action === "pause" && ["running", "queued"].includes(job.status)) {
    job.status = "paused";
  } else if (
    (action === "resume" && job.status === "paused") ||
    (action === "retry" && job.status === "failed")
  ) {
    const duplicate = next.jobs.some(
      (item) =>
        item.id !== id &&
        item.assetId === job.assetId &&
        item.type === job.type &&
        ACTIVE.has(item.status),
    );
    if (duplicate) return next;
    job.status = "queued";
    if (action === "retry") job.progress = 0;
    delete job.error;
  }
  return next;
}

export function tickJobs(state) {
  const next = copy(state);
  const configured = Number(next.settings.concurrency);
  const limits = {
    download: Number.isFinite(configured)
      ? Math.max(1, Math.floor(configured))
      : 3,
    process: 1,
  };
  const occupied = { download: 0, process: 0 };
  const autoProcess = [];
  // Preserve jobs already in flight before considering waiting work.
  const candidates = [
    ...next.jobs.filter((job) => job.status === "running"),
    ...next.jobs.filter((job) => job.status === "queued"),
  ];
  for (const job of candidates) {
    const asset = getAsset(next, job.assetId);
    const error =
      job.type === "download"
        ? downloadError(next, asset)
        : !asset || !["downloaded", "processed"].includes(asset.status)
          ? "请先下载原始素材"
          : null;
    if (error) {
      job.status = "failed";
      job.error = error;
      record(next, "error", `${asset?.title || "任务"}：${error}`);
      continue;
    }
    if (occupied[job.type] >= limits[job.type]) {
      job.status = "queued";
      continue;
    }
    occupied[job.type]++;
    job.status = "running";
    job.progress = Math.min(
      100,
      job.progress + (job.type === "download" ? 7 : 5),
    );
    if (job.progress < 100) continue;
    job.status = "completed";
    job.completedAt = now();
    if (job.type === "download") {
      if (asset.status === "discovered") asset.status = "downloaded";
      if (next.settings.autoProcess) autoProcess.push(asset.id);
      record(next, "download", `${asset.title} · 下载完成`);
    } else {
      asset.status = "processed";
      asset.versions.push(outputVersion(asset, job.preset));
      record(next, "process", `${asset.title} · 已生成新的处理版本`);
    }
  }
  for (const assetId of autoProcess) {
    if (
      !next.jobs.some(
        (job) =>
          job.assetId === assetId &&
          job.type === "process" &&
          ACTIVE.has(job.status),
      )
    ) {
      next.jobs.push(makeJob(assetId, "process", DEFAULT_PRESET));
    }
  }
  return next;
}

export function normalizeSiteUrl(input) {
  if (typeof input !== "string") return null;
  const value = input.trim();
  if (!value || /[\s\\]/.test(value)) return null;
  if (/^[a-z][a-z\d+.-]*:/i.test(value) && !/^https?:\/\//i.test(value))
    return null;
  try {
    const url = new URL(
      /^https?:\/\//i.test(value) ? value : `https://${value}`,
    );
    if (
      !["http:", "https:"].includes(url.protocol) ||
      !url.hostname ||
      url.username ||
      url.password
    )
      return null;
    if (
      !url.hostname.includes(".") &&
      url.hostname !== "localhost" &&
      !url.hostname.startsWith("[")
    )
      return null;
    url.hash = "";
    return url.href;
  } catch {
    return null;
  }
}

export function normalizeScanUrl(input) {
  if (typeof input !== "string") return null;
  let value = input.trim();
  let normalized = normalizeSiteUrl(value);
  if (!normalized) {
    const links = value.match(/https?:\/\/[^\s<>"'`]+/gi) || [];
    if (links.length !== 1) return null;
    value = links[0].replace(/[。，、；！？）】》」』]+$/u, "");
    normalized = normalizeSiteUrl(value);
  }
  if (!normalized) return null;
  const original = new URL(/^https?:\/\//i.test(value) ? value : `https://${value}`);
  return /^#(?:\/|!\/|\?)/.test(original.hash) ? normalized + original.hash : normalized;
}

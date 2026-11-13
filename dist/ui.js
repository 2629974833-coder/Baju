const paths = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  layers:
    '<path d="m12 3 10 5-10 5L2 8l10-5Zm-10 9 10 5 10-5M2 16l10 5 10-5"/>',
  download:
    '<path d="M12 3v12m-5-5 5 5 5-5M4 15v5a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-5"/>',
  wand: '<path d="m15 4 5 5L7 22l-5-5L15 4Zm-4 4 5 5M5 2v4M3 4h4m12 11v5m-2.5-2.5h5M12 1v3"/>',
  folder:
    '<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/>',
  globe:
    '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18Z"/>',
  zap: '<path d="m13 2-9 12h7l-1 8 10-13h-7l1-7Z"/>',
  settings:
    '<path d="m9 3-.5 3-2 .9-2.5-1-2 3.5 2.2 1.8v2.6L2 15.5 4 19l2.5-1 2 .9L9 22h4l.5-3.1 2-.9 2.5 1 2-3.5-2.2-1.7v-2.6L20 9.5 18 6l-2.5 1-2-.9L13 3H9Z" transform="translate(1 -1)"/><circle cx="12" cy="12" r="3"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  chevronRight: '<path d="m9 5 7 7-7 7"/>',
  arrowRight: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
  arrowUpRight: '<path d="M5 19 19 5M6 5h13v13"/>',
  external:
    '<path d="M14 3h7v7m0-7L10 14M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  checkCircle: '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
  alertCircle: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5m0 4v.1"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 10v7m0-10v.1"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2"/>',
  unlock:
    '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 7.5-2m-3.5 10v2"/>',
  shield:
    '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
  key: '<circle cx="8" cy="8" r="5"/><path d="m11.5 11.5 9 9m-4-4 3-3m-6 0 3-3"/>',
  refresh:
    '<path d="M20 7a9 9 0 0 0-15-1L2 9m0-6v6h6M4 17a9 9 0 0 0 15 1l3-3m0 6v-6h-6"/>',
  x: '<path d="m6 6 12 12M6 18 18 6"/>',
  more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  play: '<path d="m8 4 12 8-12 8V4Z"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  image:
    '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="1.5"/><path d="m21 15-5-5L6 21"/>',
  video:
    '<rect x="3" y="5" width="13" height="14" rx="2"/><path d="m16 10 5-3v10l-5-3"/>',
  sliders: '<path d="M4 7h5m4 0h7M4 17h9m4 0h3M9 4v6m4 4v6"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.1M3 12h.1M3 18h.1"/>',
  heart:
    '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z"/>',
  bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9 8a3 3 0 0 1 6 1c0 2-3 2-3 4m0 4v.1"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',
  logOut: '<path d="M9 3H4v18h5m5-13 4 4-4 4m-6-4h13"/>',
  creditCard:
    '<rect x="2" y="4" width="20" height="16" rx="3"/><path d="M2 10h20M6 15h3"/>',
  calendar:
    '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4m10-4v4M3 11h18m-13 4h2m4 0h2"/>',
  hardDrive:
    '<path d="m5 4-3 12h20L19 4H5Z"/><rect x="2" y="16" width="20" height="6" rx="2"/><path d="M6 19h.1M10 19h.1"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v4m6-4v4M9 18v4m6-4v4M2 9h4m-4 6h4m12-6h4m-4 6h4"/>',
  save: '<path d="M4 3h13l4 4v14H3V3h1Z"/><path d="M7 3v6h10V3M7 21v-8h10v8"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  link: '<path d="m10 13 4-4m-6 6-2 2a3 3 0 0 1-4-4l5-5a3 3 0 0 1 4 0m2 3a3 3 0 0 0 4 0l5-5a3 3 0 0 0-4-4l-2 2"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z"/>',
  film: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 3v18M17 3v18M3 7.5h4m10 0h4M3 12h18M3 16.5h4m10 0h4"/>',
  archive: '<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9M10 13h4"/>',
  compass: '<circle cx="12" cy="12" r="9"/><path d="m15.5 8.5-2.2 4.8-4.8 2.2 2.2-4.8z"/>',
  pulse: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  overview: '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M3 9.5h18M9.5 21V9.5"/>',
  tune: '<path d="M4 7h9M19 7h1M4 17h3M13 17h7"/><circle cx="16" cy="7" r="2.5"/><circle cx="10" cy="17" r="2.5"/>',
  columns: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M12 4v16"/>',
  frame: '<path d="M4 9V5a1 1 0 0 1 1-1h4M15 4h4a1 1 0 0 1 1 1v4M20 15v4a1 1 0 0 1-1 1h-4M9 20H5a1 1 0 0 1-1-1v-4"/>',
  sparkle:
    '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z"/>',
};
export function icon(name, size = 18) {
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.grid}</svg>`;
}
export function escapeHtml(value = "") {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
}
export function formatSize(mb = 0) {
  if (mb > 0 && mb < 1 / 1024) return `${Math.max(1, Math.round(mb * 1024 ** 2))} B`;
  if (mb > 0 && mb < 1) return `${Math.round(mb * 1024)} KB`;
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}

(() => {
  window.__framekeepAuthState = 'pending';
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  fetch('/api/capabilities', {credentials:'same-origin',cache:'no-store',signal:controller.signal})
    .then(response => response.ok ? response.json() : null)
    .then(value => { window.__framekeepAuthState = value?.mode === 'real' && value?.version === 1 ? 'ready' : 'login'; })
    .catch(() => { window.__framekeepAuthState = 'login'; })
    .finally(() => clearTimeout(timer));
})();

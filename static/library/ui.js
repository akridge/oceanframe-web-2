/** Small DOM and formatting helpers shared by the library modules. */

export const $ = id => document.getElementById(id);
export const $$ = sel => Array.from(document.querySelectorAll(sel));

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function bytes(n) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = Number(n) || 0;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return `${value.toFixed(value < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

export const count = n => (Number(n) || 0).toLocaleString();

export function when(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString();
}

/** Quality band name, kept in step with the server's facet buckets. */
export function band(quality) {
  if (quality >= 70) return 'excellent';
  if (quality >= 50) return 'good';
  if (quality >= 25) return 'fair';
  return 'poor';
}

let toastTimer = null;
export function notice(message, tone = 'info') {
  const bar = $('notice-bar');
  const text = $('notice-text');
  if (!bar || !text) return;
  text.textContent = message;
  bar.classList.remove('hidden');
  bar.dataset.tone = tone;
  if (toastTimer) clearTimeout(toastTimer);
  if (tone !== 'sticky') toastTimer = setTimeout(() => bar.classList.add('hidden'), 9000);
}

export function bindModal(id) {
  const modal = $(id);
  if (!modal) return { open() {}, close() {} };
  const close = () => modal.classList.add('hidden');
  modal.querySelectorAll('[data-close]').forEach(node => node.addEventListener('click', close));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !modal.classList.contains('hidden')) close();
  });
  return { open: () => modal.classList.remove('hidden'), close, node: modal };
}

// 24x24 stroke icons (Feather-style), kept inline so they inherit currentColor.
const ICONS = {
  cloud: 'M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z',
  folder: 'M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z',
  record: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M8 13h8M8 17h5',
  calendar: 'M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zM16 2v4M8 2v4M3 10h18',
  flag: 'M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1zM4 22v-7',
  pin: 'M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0zM12 13a3 3 0 1 0 0-6 3 3 0 0 0 0 6z',
  user: 'M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z',
  image: 'M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zM8.5 10a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3zM21 15l-5-5L5 21',
  target: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 18a6 6 0 1 0 0-12 6 6 0 0 0 0 12zM12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4z',
  clock: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2',
  cpu: 'M6 4h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zM9 9h6v6H9zM9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3',
  layers: 'M12 2 2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5',
  tag: 'M20.59 13.41 13.42 20.58a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82zM7 7h.01',
};

export function icon(name, cls = 'icon') {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', cls);
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(ns, 'path');
  path.setAttribute('d', ICONS[name] || '');
  svg.append(path);
  return svg;
}

/** "2025", "2025–2026", or '' for a collection's year range. */
export function yearRange(source) {
  const start = source?.year_start;
  const end = source?.year_end;
  if (!start) return '';
  return end && end !== start ? `${start}–${end}` : String(start);
}

/** Only https links from stored metadata become anchors. */
export function safeLink(url, text, attrs = {}) {
  let ok = false;
  try { ok = new URL(url).protocol === 'https:'; } catch { ok = false; }
  return ok
    ? el('a', { href: url, target: '_blank', rel: 'noopener noreferrer', text: text || url, ...attrs })
    : el('span', { text: text || url || '' });
}

export function renderStatusChips(status) {
  const assets = $('status-assets');
  const embedderChip = $('status-embedder');
  if (!assets || !embedderChip) return;
  const stats = status.stats;
  const embedder = status.embedder || {};
  assets.textContent = `${count(stats.ok)} assets · ${count(stats.folders)} folders · ${bytes(stats.bytes)}`;
  embedderChip.textContent = embedder.supports_text
    ? `semantic search on (${embedder.backend})`
    : 'image similarity only';
  embedderChip.title = embedder.detail || '';
}

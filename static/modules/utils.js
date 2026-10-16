export const $ = id => document.getElementById(id);

export function fmtTime(ms) {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  if (h > 0) return `${h}:${String(m % 60).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  return `${m}:${String(s % 60).padStart(2, '0')}`;
}

export function fmtScore(v) {
  return Number(v).toFixed(1);
}

export function niceMax(v) {
  if (v <= 0) return 10;
  const mag = Math.pow(10, Math.floor(Math.log10(v)));
  return Math.ceil(v / mag) * mag;
}

export function hammingHex(a, b) {
  let d = 0;
  for (let i = 0; i < a.length; i += 2) {
    let b_ = parseInt(a.substr(i, 2), 16) ^ parseInt(b.substr(i, 2), 16);
    while (b_) {
      d += b_ & 1;
      b_ >>= 1;
    }
  }
  return d;
}

export function qualityClass(score) {
  return score >= 60 ? 'q-high' : score >= 20 ? 'q-medium' : 'q-low';
}

export function extOf(file) {
  return '.' + file.name.split('.').pop().toLowerCase();
}

export function showSection(el) {
  el?.classList.remove('hidden');
}

export function hideSection(el) {
  el?.classList.add('hidden');
}

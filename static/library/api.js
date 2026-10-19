/**
 * Thin wrapper over the /api/library endpoints.
 * Every helper throws an Error carrying the server's `detail` so callers can
 * surface the actionable message (missing weights, no credentials, …) verbatim.
 */

import { authFetch } from '../auth/session.js';

const BASE = '/api/library';

async function request(path, options = {}) {
  const res = await authFetch(BASE + path, options);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && body.detail) detail = body.detail;
    } catch { /* non-JSON error body */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

const json = (path, body, method = 'POST') => request(path, {
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body ?? {}),
});

export const api = {
  status:       ()               => request('/status'),
  search:       query            => json('/search', query),
  facets:       query            => json('/facets', query),
  folders:      (query, prefix)  => json('/folders', { query, prefix }),
  duplicates:   (query, limit)   => json('/duplicates', { query, limit }),
  asset:        id               => request(`/asset/${id}`),
  curationLinks: async uri      => {
    const res = await authFetch(`/api/curation/library-links?uri=${encodeURIComponent(uri)}`);
    return res.ok ? (await res.json()).links : [];
  },

  addSource:    body             => json('/sources', body),
  source:       id               => request(`/sources/${id}`),
  updateSourceMeta: (id, meta)   => json(`/sources/${id}/meta`, meta, 'PATCH'),
  inportLookup: url              => json('/inport/lookup', { url }),
  inportRefresh: id              => json(`/sources/${id}/inport/refresh`),
  removeSource: id               => request(`/sources/${id}`, { method: 'DELETE' }),
  index:        body             => json('/index', body),
  embed:        rebuild          => json('/embed', { rebuild }),
  annotate:     body             => json('/annotate', body),

  models:       ()               => request('/models'),
  jobs:         ()               => request('/jobs'),
  cancelJob:    id               => json(`/jobs/${id}/cancel`),

  tags:         ()               => request('/tags'),
  addTags:      body             => json('/tags', body),
  removeTags:   body             => json('/tags/remove', body),

  datasets:     ()               => request('/datasets'),
  createDataset: body            => json('/datasets', body),
  deleteDataset: id              => request(`/datasets/${id}`, { method: 'DELETE' }),
  exportDataset: (id, body)      => json(`/datasets/${id}/export`, body),
  exports:      ()               => request('/exports'),

  searchByImage(file, query) {
    const form = new FormData();
    form.append('file', file);
    return request(`/search-by-image?query=${encodeURIComponent(JSON.stringify(query || {}))}`, {
      method: 'POST', body: form,
    });
  },
};

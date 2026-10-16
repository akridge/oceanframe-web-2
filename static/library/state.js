/**
 * Query + selection state.
 *
 * The query object is the single source of truth: the folder tree, facets,
 * sliders and search box all mutate it, and every panel re-reads it. Keeping
 * one shape means it can be saved, replayed, and handed straight to a dataset.
 */

const listeners = new Set();

export const state = {
  query: {
    text: '', mode: 'auto', keywords: '', similar_to: null, vector: null,
    folder: '', source_id: null, year: null, survey: '', region: '',
    tags: [], exclude_tags: [], labels: [], label_conf: 0.25,
    quality_min: null, dedupe: false, untagged: false, unannotated: false,
    sort: 'quality', page: 0, page_size: 120, status: 'ok',
  },
  results: { items: [], total: 0, matched: 0, note: '' },
  selection: new Set(),
  facets: { tags: [], labels: [], quality: {} },
  status: null,
};

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function emit(event = 'change') {
  listeners.forEach(fn => fn(event));
}

/** Merge into the query. Any change except paging resets to page 0. */
export function setQuery(patch, { keepPage = false } = {}) {
  Object.assign(state.query, patch);
  if (!keepPage && !('page' in patch)) state.query.page = 0;
  emit('query');
}

export function toggleInList(key, value) {
  const list = state.query[key];
  const index = list.indexOf(value);
  if (index >= 0) list.splice(index, 1);
  else list.push(value);
  state.query.page = 0;
  emit('query');
}

export function clearVectorQuery() {
  state.query.similar_to = null;
  state.query.vector = null;
}

/** Query stripped of the transient bits that should not be saved or replayed. */
export function persistableQuery() {
  const { vector, page, ...rest } = state.query;
  return rest;
}

export function toggleSelected(id) {
  if (state.selection.has(id)) state.selection.delete(id);
  else state.selection.add(id);
  emit('selection');
}

export function selectMany(ids) {
  ids.forEach(id => state.selection.add(id));
  emit('selection');
}

export function clearSelection() {
  state.selection.clear();
  emit('selection');
}

/** Result grid: tiles, selection, paging. */
import { el, clear, $, band, count } from './ui.js';
import { state, toggleSelected, selectMany, clearSelection, emit } from './state.js';

export function createGrid({ onOpen, activeFilters = () => [] }) {
  const grid = $('grid');
  const empty = $('grid-empty');

  /** From the start of a search until the newest one lands; the old tiles stay, dimmed. */
  function setLoading(on) {
    grid.classList.toggle('loading', on);
    $('result-count').classList.toggle('loading', on);
    if (on) $('result-count').textContent = 'Searching…';
  }

  function render() {
    const { items, total, matched, note } = state.results;
    setLoading(false);
    clear(grid);

    $('result-count').textContent = state.query.similar_to || state.query.vector
      ? `${count(matched)} ranked of ${count(total)}`
      : `${count(matched || total)} images`;
    $('result-note').textContent = note || '';

    if (!items.length) {
      empty.classList.remove('hidden');
      clear(empty).append(el('p', { text: emptyMessage() }));
      const filters = state.status && state.status.stats.assets ? activeFilters() : [];
      if (filters.length) {
        // Say which filters emptied it, and undo the newest in one click.
        const [lastText, dropLast] = filters[filters.length - 1];
        empty.append(
          el('p', { class: 'empty-filters', text: `Filters: ${filters.map(([text]) => text).join(' · ')}` }),
          el('button', { type: 'button', class: 'btn btn-secondary btn-sm', text: `Remove “${lastText}”`, onclick: dropLast }),
        );
      }
      updatePager();
      return;
    }
    empty.classList.add('hidden');

    for (const item of items) grid.append(tile(item));
    updatePager();
  }

  function emptyMessage() {
    if (!state.status || !state.status.stats.assets) {
      return 'Nothing indexed yet — open “Index” and point the library at a bucket or a folder.';
    }
    return 'No images match these filters.';
  }

  function tile(item) {
    const selected = state.selection.has(item.id);
    const check = el('input', {
      type: 'checkbox', class: 'tile-check', title: 'Select',
      onclick: event => { event.stopPropagation(); toggleSelected(item.id); },
    });
    check.checked = selected;

    const node = el('div', {
      class: `tile${selected ? ' selected' : ''}`,
      dataset: { id: item.id },
      onclick: event => {
        // Shift-click extends the selection; a plain click opens the asset.
        if (event.shiftKey || event.metaKey || event.ctrlKey) toggleSelected(item.id);
        else onOpen(item.id);
      },
    }, [
      el('div', { class: 'tile-media' }, [
        el('img', {
          class: 'tile-img', src: item.thumb, alt: item.name, loading: 'lazy', decoding: 'async',
        }),
        // Which collection this came from only matters once a library holds
        // more than one, so it stays out of the way until hover.
        item.source ? el('div', { class: 'tile-source', text: item.source, title: item.source }) : null,
      ]),
      el('span', { class: `quality-badge ${band(item.quality)}`, text: Math.round(item.quality), title: 'OceanFrame quality score' }),
      item.score !== undefined ? el('span', { class: 'score-badge', text: item.score.toFixed(2), title: 'Similarity' }) : null,
      check,
      el('div', { class: 'tile-body' }, [
        el('div', { class: 'tile-name', text: item.name, title: item.name }),
        el('div', { class: 'tile-folder', text: item.folder || '/', title: item.folder }),
        (item.labels && item.labels.length)
          ? el('div', { class: 'tile-labels' }, item.labels.slice(0, 3).map(l => el('span', { class: 'tile-label', text: l })))
          : null,
      ]),
    ]);
    return node;
  }

  function updatePager() {
    const { matched, total } = state.results;
    const size = state.query.page_size;
    const shown = matched || total;
    const pages = Math.max(1, Math.ceil(shown / size));
    $('page-label').textContent = `Page ${state.query.page + 1} of ${pages}`;
    $('page-prev').disabled = state.query.page === 0;
    $('page-next').disabled = state.query.page + 1 >= pages;
  }

  // Shadow under the sticky toolbar only once the grid has scrolled beneath it.
  const toolbar = document.querySelector('.toolbar');
  if (toolbar) {
    window.addEventListener('scroll', () => {
      toolbar.classList.toggle('stuck', window.scrollY > 12);
    }, { passive: true });
  }

  $('select-page').addEventListener('click', () => selectMany(state.results.items.map(i => i.id)));
  $('select-none').addEventListener('click', clearSelection);
  $('page-prev').addEventListener('click', () => { state.query.page -= 1; emit('query'); });
  $('page-next').addEventListener('click', () => { state.query.page += 1; emit('query'); });

  return { render, setLoading };
}

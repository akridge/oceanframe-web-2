/**
 * Left rail: folder tree, facets, quality controls, and the active-filter chips.
 *
 * The tree is a facet, not a file browser — every count is recomputed against
 * the current query, so drilling into 2024/kaneohe tells you how many *matching*
 * images are down each branch.
 *
 * The tree's browse position follows the folder filter: every way of changing
 * the folder goes through setQuery, and refresh() re-reads it from there.
 * Only ".." moves the browse position on its own, to look without filtering.
 */
import { $, el, clear, count, yearRange } from './ui.js';
import { api } from './api.js';
import { state, setQuery, toggleInList, emit } from './state.js';

export function createFilters({ collectionInfo }) {
  const treeBox = $('folder-tree');
  const crumbs = $('folder-crumbs');
  let browsePrefix = '';
  let treeToken = 0;
  let facetToken = 0;

  // ── Folder tree ─────────────────────────────────────────────────────────

  async function renderTree(facetsReady) {
    const token = ++treeToken;
    treeBox.classList.add('stale');
    const known = state.status ? state.status.sources : [];
    if (!state.query.source_id && !browsePrefix && known.length > 1) {
      // Folders belong to a collection, so with none picked the top level is the collections.
      const facets = await (facetsReady || Promise.resolve(state.facets));
      if (token !== treeToken) return;
      const counts = new Map(((facets && facets.sources) || []).map(s => [s.id, s.count]));
      renderCrumbs();
      clear(treeBox).classList.remove('stale');
      for (const source of known) {
        treeBox.append(el('li', {}, [row(source.label || source.root, source.root, counts.get(source.id) || 0,
          () => setQuery({ source_id: source.id, folder: '', folder_exact: false }))]));
      }
      return;
    }

    const query = { ...state.query, folder: '', similar_to: null, vector: null, page: 0 };
    let data;
    try {
      data = await api.folders(query, browsePrefix);
    } catch {
      if (token !== treeToken) return;
      clear(treeBox).classList.remove('stale');
      treeBox.append(el('li', { class: 'muted-row', text: 'Folder counts unavailable' }));
      return;
    }
    if (token !== treeToken) return;          // a newer refresh is on its way

    renderCrumbs();
    clear(treeBox).classList.remove('stale');

    if (browsePrefix) {
      treeBox.append(el('li', {}, [row('..', parentOf(browsePrefix), null, () => {
        browsePrefix = parentOf(browsePrefix);
        renderTree();
      })]));
      if (data.here) {
        treeBox.append(el('li', {}, [row('· images here', browsePrefix, data.here, () => {
          setQuery({ folder: browsePrefix, folder_exact: true });
        }, state.query.folder === browsePrefix && state.query.folder_exact)]));
      }
    }

    if (!data.children.length && !data.here) {
      treeBox.append(el('li', { class: 'muted-row', text: 'No sub-folders' }));
    }

    for (const child of data.children) {
      treeBox.append(el('li', {}, [row(child.name, child.path, child.count, () => {
        setQuery({ folder: child.path, folder_exact: false });
      }, state.query.folder === child.path && !state.query.folder_exact)]));
    }
  }

  function row(name, path, n, onClick, active = false) {
    return el('button', { type: 'button', class: `folder-row${active ? ' active' : ''}`, onclick: onClick }, [
      el('span', { class: 'name', text: name, title: path }),
      n === null ? null : el('span', { class: 'count', text: count(n) }),
    ]);
  }

  function parentOf(path) {
    const cut = path.lastIndexOf('/');
    return cut < 0 ? '' : path.slice(0, cut);
  }

  function renderCrumbs() {
    clear(crumbs);
    const picked = state.query.source_id
      ? (state.status?.sources || []).find(s => s.id === state.query.source_id) : null;
    crumbs.append(el('button', {
      type: 'button', text: picked ? picked.label || picked.root : 'all',
      title: picked ? 'Top of this collection' : 'All collections',
      onclick: () => setQuery({ folder: '', folder_exact: false }),
    }));
    let accumulated = '';
    for (const segment of browsePrefix.split('/').filter(Boolean)) {
      accumulated = accumulated ? `${accumulated}/${segment}` : segment;
      const target = accumulated;
      crumbs.append(el('span', { text: '/' }));
      crumbs.append(el('button', {
        type: 'button', text: segment,
        onclick: () => setQuery({ folder: target, folder_exact: false }),
      }));
    }
  }

  // ── Facets ──────────────────────────────────────────────────────────────

  const facetBoxes = () => ['source-filter', 'facet-tags', 'facet-labels', 'quality-bands'].map($).filter(Boolean);

  async function renderFacets() {
    const token = ++facetToken;
    facetBoxes().forEach(box => box.classList.add('stale'));
    let data;
    try {
      data = await api.facets(state.query);
    } catch {
      if (token === facetToken) facetBoxes().forEach(box => box.classList.remove('stale'));
      return null;
    }
    if (token !== facetToken) return data;     // the tree may still use it; the rail waits for the newest
    state.facets = data;
    facetBoxes().forEach(box => box.classList.remove('stale'));

    renderSources(data.sources || []);
    renderSurveyFacets(data);

    const tagBox = clear($('facet-tags'));
    if (!data.tags.length) tagBox.append(el('span', { class: 'muted-row', text: 'No tags yet' }));
    for (const tag of data.tags) {
      const on = state.query.tags.includes(tag.name);
      tagBox.append(el('button', {
        type: 'button', class: `facet${on ? ' on' : ''}`, dataset: { kind: tag.kind },
        onclick: () => toggleInList('tags', tag.name),
      }, [tag.name, el('span', { class: 'n', text: count(tag.count) })]));
    }

    const labelBox = clear($('facet-labels'));
    if (!data.labels.length) {
      labelBox.append(el('span', { class: 'muted-row', text: 'Run YOLO or SAM 3 to populate' }));
    }
    for (const label of data.labels) {
      const on = state.query.labels.includes(label.name);
      labelBox.append(el('button', {
        type: 'button', class: `facet${on ? ' on' : ''}`, dataset: { kind: 'class' },
        onclick: () => toggleInList('labels', label.name),
      }, [label.name, el('span', { class: 'n', text: count(label.count) })]));
    }

    const bands = clear($('quality-bands'));
    const labels = { poor: '0–25', fair: '25–50', good: '50–70', excellent: '70+' };
    for (const [key, text] of Object.entries(labels)) {
      bands.append(el('button', {
        type: 'button', class: `band ${key}`, title: `Quality ${text}`,
        onclick: () => setQuery(rangeFor(key)),
      }, [el('b', { text: count(data.quality[key] || 0) }), text]));
    }
    return data;
  }

  function renderSources(sources) {
    // Shown even for one collection: the (i) button is the way to its metadata.
    const card = $('collections-card');
    const box = clear($('source-filter'));
    const known = state.status ? state.status.sources : [];
    if (!known.length) {
      card.classList.add('hidden');
      return;
    }
    card.classList.remove('hidden');

    // Show every configured collection, including ones the current query
    // filters down to zero, so you can always switch back to them.
    const counts = new Map(sources.map(s => [s.id, s.count]));
    for (const source of known) {
      const on = state.query.source_id === source.id;
      const years = yearRange(source);
      box.append(el('div', { class: 'source-item' }, [
        el('button', {
          type: 'button', class: `source-btn${on ? ' on' : ''}`, title: source.root,
          onclick: () => setQuery({ source_id: on ? null : source.id, folder: '', folder_exact: false }),
        }, [
          el('span', { class: 'kind', text: source.kind }),
          el('span', { class: 'label' }, [
            source.label || source.root,
            years ? el('span', { class: 'years', text: ` · ${years}` }) : null,
          ]),
          el('span', { class: 'count', text: count(counts.get(source.id) || 0) }),
        ]),
        el('button', {
          type: 'button', class: 'info-btn', text: 'i', title: 'Collection details',
          'aria-label': `Details for ${source.label || source.root}`,
          onclick: () => collectionInfo.open(source.id),
        }),
      ]));
    }
  }

  function renderSurveyFacets(data) {
    const groups = [
      ['years', 'year', data.years || []],
      ['surveys', 'survey', data.surveys || []],
      ['regions', 'region', data.regions || []],
    ];
    let any = false;
    for (const [id, key, items] of groups) {
      const box = clear($(`facet-${id}`));
      $(`facet-${id}-wrap`).classList.toggle('hidden', !items.length);
      for (const item of items) {
        any = true;
        const on = state.query[key] === item.name;
        box.append(el('button', {
          type: 'button', class: `facet${on ? ' on' : ''}`,
          onclick: () => setQuery({ [key]: on ? (key === 'year' ? null : '') : item.name }),
        }, [String(item.name), el('span', { class: 'n', text: count(item.count) })]));
      }
    }
    // Keep the card while a survey filter is active, even if it narrowed things to nothing.
    const active = state.query.year !== null || state.query.survey || state.query.region;
    $('survey-card').classList.toggle('hidden', !any && !active);
  }

  function renderBanner() {
    const id = state.query.source_id;
    const source = id ? (state.status?.sources || []).find(s => s.id === id) : null;
    collectionInfo.renderBanner(source || null);
  }

  function rangeFor(key) {
    return {
      poor:      { quality_min: null, quality_max: 25 },
      fair:      { quality_min: 25, quality_max: 50 },
      good:      { quality_min: 50, quality_max: 70 },
      excellent: { quality_min: 70, quality_max: null },
    }[key];
  }

  // ── Active filter chips ─────────────────────────────────────────────────

  /** Every active filter as [text, remove, class]; the grid's empty state names them too. */
  function chipList() {
    const chips = [];
    const q = state.query;

    if (q.text) {
      chips.push([`search: “${q.text}”${q.sort === 'relevance' ? ' · sorted by relevance' : ''}`,
        () => setQuery({ text: '', sort: q.sort === 'relevance' ? 'quality' : q.sort }), 'search-chip']);
    }
    if (q.similar_to) chips.push(['Similar to #' + q.similar_to, () => setQuery({ similar_to: null })]);
    if (q.vector) chips.push(['Similar to uploaded image', () => setQuery({ vector: null })]);
    if (q.source_id) {
      const source = (state.status?.sources || []).find(s => s.id === q.source_id);
      // Folders are relative to a collection, so leaving it leaves its folder too.
      chips.push([`collection: ${source ? source.label || source.root : q.source_id}`,
        () => setQuery({ source_id: null, folder: '', folder_exact: false })]);
    }
    if (q.year !== null && q.year !== undefined) chips.push([`year: ${q.year}`, () => setQuery({ year: null })]);
    if (q.survey) chips.push([`survey: ${q.survey}`, () => setQuery({ survey: '' })]);
    if (q.region) chips.push([`region: ${q.region}`, () => setQuery({ region: '' })]);
    if (q.folder) chips.push([`folder: ${q.folder}${q.folder_exact ? ' (only)' : '/…'}`, () => setQuery({ folder: '', folder_exact: false })]);
    for (const tag of q.tags) chips.push([tag, () => toggleInList('tags', tag)]);
    for (const label of q.labels) chips.push([`class: ${label}`, () => toggleInList('labels', label)]);
    if (q.quality_min !== null && q.quality_min !== undefined) chips.push([`quality ≥ ${q.quality_min}`, () => setQuery({ quality_min: null })]);
    if (q.quality_max !== null && q.quality_max !== undefined) chips.push([`quality ≤ ${q.quality_max}`, () => setQuery({ quality_max: null })]);
    if (q.dedupe) chips.push(['deduped', () => setQuery({ dedupe: false })]);
    if (q.untagged) chips.push(['untagged only', () => setQuery({ untagged: false })]);
    if (q.unannotated) chips.push(['un-annotated only', () => setQuery({ unannotated: false })]);
    return chips;
  }

  function renderChips() {
    const box = clear($('active-filters'));
    for (const [text, onRemove, extra] of chipList()) {
      box.append(el('span', { class: `filter-chip${extra ? ` ${extra}` : ''}` }, [
        text, el('button', { type: 'button', text: '×', title: 'Remove', onclick: onRemove }),
      ]));
    }
  }

  // ── Controls ────────────────────────────────────────────────────────────

  const qualitySlider = $('quality-min');
  qualitySlider.addEventListener('input', () => {
    $('quality-min-val').textContent = qualitySlider.value;
  });
  qualitySlider.addEventListener('change', () => {
    const value = Number(qualitySlider.value);
    setQuery({ quality_min: value > 0 ? value : null });
  });

  const confSlider = $('label-conf');
  confSlider.addEventListener('input', () => { $('label-conf-val').textContent = Number(confSlider.value).toFixed(2); });
  confSlider.addEventListener('change', () => setQuery({ label_conf: Number(confSlider.value) }));

  $('filter-dedupe').addEventListener('change', e => setQuery({ dedupe: e.target.checked }));
  $('filter-untagged').addEventListener('change', e => setQuery({ untagged: e.target.checked }));
  $('filter-unannotated').addEventListener('change', e => setQuery({ unannotated: e.target.checked }));
  $('quality-reset').addEventListener('click', () => {
    qualitySlider.value = 0;
    $('quality-min-val').textContent = '0';
    setQuery({ quality_min: null, quality_max: null });
  });
  $('sources-reset').addEventListener('click', () => setQuery({ source_id: null, folder: '', folder_exact: false }));
  $('survey-reset').addEventListener('click', () => setQuery({ year: null, survey: '', region: '' }));
  $('tags-reset').addEventListener('click', () => setQuery({ tags: [], exclude_tags: [] }));
  $('labels-reset').addEventListener('click', () => setQuery({ labels: [] }));
  $('folder-reset').addEventListener('click', () => setQuery({ folder: '', folder_exact: false }));

  function syncControls() {
    qualitySlider.value = state.query.quality_min ?? 0;
    $('quality-min-val').textContent = String(state.query.quality_min ?? 0);
    $('filter-dedupe').checked = !!state.query.dedupe;
    $('filter-untagged').checked = !!state.query.untagged;
    $('filter-unannotated').checked = !!state.query.unannotated;
  }

  return {
    refresh() {
      syncControls(); renderChips(); renderBanner();
      browsePrefix = state.query.folder || '';
      renderTree(renderFacets());
    },
    renderChips,
    chips: chipList,
  };
}

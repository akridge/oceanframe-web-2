/**
 * Left rail: folder tree, facets, quality controls, and the active-filter chips.
 *
 * The tree is a facet, not a file browser — every count is recomputed against
 * the current query, so drilling into 2024/kaneohe tells you how many *matching*
 * images are down each branch.
 */
import { $, el, clear, count, yearRange } from './ui.js';
import { api } from './api.js';
import { state, setQuery, toggleInList, emit } from './state.js';

export function createFilters({ collectionInfo }) {
  const treeBox = $('folder-tree');
  const crumbs = $('folder-crumbs');
  let browsePrefix = '';

  // ── Folder tree ─────────────────────────────────────────────────────────

  async function renderTree() {
    const query = { ...state.query, folder: '', similar_to: null, vector: null, page: 0 };
    let data;
    try {
      data = await api.folders(query, browsePrefix);
    } catch {
      clear(treeBox).append(el('li', { class: 'muted-row', text: 'Folder counts unavailable' }));
      return;
    }

    renderCrumbs();
    clear(treeBox);

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
        browsePrefix = child.path;
        setQuery({ folder: child.path, folder_exact: false });
        renderTree();
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
    crumbs.append(el('button', {
      type: 'button', text: 'all',
      onclick: () => { browsePrefix = ''; setQuery({ folder: '', folder_exact: false }); renderTree(); },
    }));
    let accumulated = '';
    for (const segment of browsePrefix.split('/').filter(Boolean)) {
      accumulated = accumulated ? `${accumulated}/${segment}` : segment;
      const target = accumulated;
      crumbs.append(el('span', { text: '/' }));
      crumbs.append(el('button', {
        type: 'button', text: segment,
        onclick: () => { browsePrefix = target; setQuery({ folder: target, folder_exact: false }); renderTree(); },
      }));
    }
  }

  // ── Facets ──────────────────────────────────────────────────────────────

  async function renderFacets() {
    let data;
    try {
      data = await api.facets(state.query);
    } catch {
      return;
    }
    state.facets = data;

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

  function renderChips() {
    const box = clear($('active-filters'));
    const chips = [];
    const q = state.query;

    if (q.similar_to) chips.push(['Similar to #' + q.similar_to, () => setQuery({ similar_to: null })]);
    if (q.vector) chips.push(['Similar to uploaded image', () => setQuery({ vector: null })]);
    if (q.source_id) {
      const source = (state.status?.sources || []).find(s => s.id === q.source_id);
      chips.push([`collection: ${source ? source.label || source.root : q.source_id}`, () => setQuery({ source_id: null })]);
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

    for (const [text, onRemove] of chips) {
      box.append(el('span', { class: 'filter-chip' }, [
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
  $('sources-reset').addEventListener('click', () => setQuery({ source_id: null }));
  $('survey-reset').addEventListener('click', () => setQuery({ year: null, survey: '', region: '' }));
  $('tags-reset').addEventListener('click', () => setQuery({ tags: [], exclude_tags: [] }));
  $('labels-reset').addEventListener('click', () => setQuery({ labels: [] }));
  $('folder-reset').addEventListener('click', () => {
    browsePrefix = '';
    setQuery({ folder: '', folder_exact: false });
    renderTree();
  });

  function syncControls() {
    qualitySlider.value = state.query.quality_min ?? 0;
    $('quality-min-val').textContent = String(state.query.quality_min ?? 0);
    $('filter-dedupe').checked = !!state.query.dedupe;
    $('filter-untagged').checked = !!state.query.untagged;
    $('filter-unannotated').checked = !!state.query.unannotated;
  }

  return {
    refresh() { syncControls(); renderChips(); renderBanner(); renderTree(); renderFacets(); },
    renderChips,
    setBrowsePrefix(prefix) { browsePrefix = prefix; renderTree(); },
  };
}

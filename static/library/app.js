/**
 * OceanFrame Library — wiring.
 *
 * One query object drives everything: changing a filter, a facet, the folder
 * scope or the search box emits 'query', which re-runs the search and refreshes
 * the rail. Selection is separate so tagging never disturbs the result set.
 */
'use strict';

import { $, notice, renderStatusChips } from './ui.js';
import { api } from './api.js';
import { state, subscribe, setQuery, emit, clearSelection, clearVectorQuery } from './state.js';
import { createGrid } from './grid.js';
import { createFilters } from './filters.js';
import { createDetail } from './detail.js';
import { createCollectionInfo } from './collection-info.js';
import { createDatasets } from './datasets.js';
import { watchJob } from './jobs.js';

const detail = createDetail();
const grid = createGrid({ onOpen: id => detail.open(id) });
const collectionInfo = createCollectionInfo();
const filters = createFilters({ collectionInfo });
const datasets = createDatasets();

let searchToken = 0;

// ── Search ────────────────────────────────────────────────────────────────

async function runSearch() {
  const token = ++searchToken;
  try {
    const results = await api.search(state.query);
    if (token !== searchToken) return;      // a newer search already landed
    state.results = results;
    grid.render();
    if (results.note) notice(results.note);
  } catch (error) {
    notice(error.message, 'sticky');
  }
}

subscribe(event => {
  if (event === 'query') {
    runSearch();
    filters.refresh();
  } else if (event === 'selection') {
    renderSelection();
    grid.render();
  }
});

// ── Selection bar ─────────────────────────────────────────────────────────

function renderSelection() {
  const bar = $('action-bar');
  const n = state.selection.size;
  $('selection-count').textContent = `${n.toLocaleString()} selected`;
  bar.classList.toggle('hidden', n === 0);
  // The hint is only useful while there is nothing selected; once the bar is
  // up it is saying something the user has already worked out.
  $('select-hint').classList.toggle('hidden', n > 0);
}

function selectedIds() {
  return Array.from(state.selection);
}

$('tag-add').addEventListener('click', async () => {
  const names = splitNames($('tag-input').value);
  if (!names.length) { notice('Type one or more tag names first.'); return; }
  try {
    await api.addTags({ asset_ids: selectedIds(), names });
    $('tag-input').value = '';
    notice(`Tagged ${state.selection.size} images.`);
    emit('query');
  } catch (error) { notice(error.message, 'sticky'); }
});

$('tag-remove').addEventListener('click', async () => {
  const names = splitNames($('tag-input').value);
  if (!names.length) { notice('Type the tag to remove.'); return; }
  try {
    await api.removeTags({ asset_ids: selectedIds(), names });
    emit('query');
  } catch (error) { notice(error.message, 'sticky'); }
});

$('annotate-run').addEventListener('click', () => {
  const modelRef = $('annotate-model').value;
  // The annotator follows from the chosen model, so there is no way to pair a
  // SAM 3 checkpoint with the YOLO runner by mistake.
  const chosen = availableModels.find(m => m.ref === modelRef);
  const annotator = chosen ? chosen.kind : 'yolo';
  const prompts = splitNames($('annotate-prompts').value);
  if (annotator === 'sam3' && !prompts.length) {
    notice('SAM 3 needs concepts to look for — e.g. "fish, bleached coral".');
    return;
  }
  api.annotate({ annotator, asset_ids: selectedIds(), prompts, model: modelRef })
    .then(job => watchJob(job, { onDone: () => { refreshStatus(); emit('query'); } }))
    .catch(error => notice(error.message, 'sticky'));
});

$('dataset-create').addEventListener('click', async () => {
  const name = $('dataset-name').value.trim();
  if (!name) { notice('Give the dataset a name first.'); return; }
  try {
    const dataset = await api.createDataset({ name, asset_ids: selectedIds(), split_mode: 'by_folder' });
    $('dataset-name').value = '';
    notice(`Built “${dataset.name}” with ${dataset.size} images.`);
    datasets.open();
  } catch (error) { notice(error.message, 'sticky'); }
});

$('selection-clear').addEventListener('click', clearSelection);

function splitNames(raw) {
  return raw.split(',').map(s => s.trim()).filter(Boolean);
}

// ── Search bar ────────────────────────────────────────────────────────────

function submitSearch() {
  clearVectorQuery();
  setQuery({
    text: $('search-text').value.trim(),
    mode: $('search-mode').value,
    sort: $('search-text').value.trim() ? 'relevance' : state.query.sort,
  });
}

$('search-btn').addEventListener('click', submitSearch);
$('search-text').addEventListener('keydown', event => { if (event.key === 'Enter') submitSearch(); });
$('search-mode').addEventListener('change', submitSearch);
$('sort-select').addEventListener('change', event => setQuery({ sort: event.target.value }));

$('search-image-btn').addEventListener('click', () => $('search-image-input').click());
$('search-image-input').addEventListener('change', async event => {
  const file = event.target.files[0];
  if (!file) return;
  try {
    const results = await api.searchByImage(file, { ...state.query, page: 0, similar_to: null });
    state.results = results;
    state.query.vector = [];                // marks the chip; the server did the embedding
    grid.render();
    filters.renderChips();
    notice(`Ranked ${results.matched} images against ${file.name}.`);
  } catch (error) {
    notice(error.message, 'sticky');
  } finally {
    event.target.value = '';
  }
});

// ── Header / drawers ──────────────────────────────────────────────────────

$('open-datasets').addEventListener('click', event => { event.preventDefault(); datasets.open(); });
$('notice-close').addEventListener('click', () => $('notice-bar').classList.add('hidden'));

// ── Bootstrap ─────────────────────────────────────────────────────────────

let availableModels = [];

/**
 * Fill the selection-bar model picker from the server's allowlist.
 *
 * A COCO checkpoint is worse than useless on reef imagery, so making a domain
 * model one click away matters more than it looks — and offering a fixed list
 * rather than a text box keeps an arbitrary pickle path out of the request.
 */
async function loadModels() {
  try {
    availableModels = (await api.models()).models || [];
  } catch {
    availableModels = [];
  }
  const select = $('annotate-model');
  while (select.firstChild) select.removeChild(select.firstChild);
  const blank = document.createElement('option');
  blank.value = '';
  blank.textContent = 'default model';
  select.append(blank);
  for (const model of availableModels) {
    const option = document.createElement('option');
    option.value = model.ref;
    // SAM 3 belongs here and nowhere else: this is the one place the action is
    // scoped to images you picked by hand.
    option.textContent = model.label + (model.usable ? '' : ' — weights missing');
    option.title = model.note || '';
    option.disabled = !model.usable;
    select.append(option);
  }
  syncPromptField();
}

/** SAM 3 is prompt-driven and segments; YOLO ignores the field and detects. */
function syncPromptField() {
  const chosen = availableModels.find(m => m.ref === $('annotate-model').value);
  const isSam3 = chosen && chosen.kind === 'sam3';
  const prompts = $('annotate-prompts');
  prompts.classList.toggle('required', Boolean(isSam3));
  prompts.placeholder = isSam3
    ? 'concepts to find — required'
    : 'concepts (SAM 3 only)';
  prompts.title = isSam3
    ? 'Noun phrases SAM 3 will find and segment, e.g. "bleached coral, sea urchin".'
    : 'Only used by SAM 3. YOLO detects the classes it was trained on.';

  // The verb should match what will actually happen.
  const run = $('annotate-run');
  run.textContent = isSam3 ? 'Segment' : 'Detect';
  run.title = isSam3
    ? 'Run SAM 3 over the selected images, segmenting every instance of the concepts above.'
    : 'Run the selected detection model over the selected images.';
}

async function refreshStatus() {
  try {
    const status = await api.status();
    state.status = status;
    renderStatusChips(status);
    $('annotate-prompts').placeholder = (status.config.sam3_prompts || []).join(', ') || 'fish, coral';
    if (!status.sources.length) {
      window.location.assign('/collections/new');
      return status;
    }
    if (!status.stats.assets) {
      notice('No images indexed yet — check “My collections” for indexing progress.', 'sticky');
    } else if (!status.embedder.supports_text) {
      notice(status.embedder.detail, 'sticky');
    }
    return status;
  } catch (error) {
    notice(error.message, 'sticky');
    return null;
  }
}

$('annotate-model').addEventListener('change', syncPromptField);

$('sort-select').value = state.query.sort;
renderSelection();
loadModels();
refreshStatus().then(() => emit('query'));

// The "All" tab is current only while the Datasets drawer is closed.
{
  const drawer = $('datasets');
  const allTab = document.querySelector('.topbar .tabs a[href="/"]');
  const datasetsTab = $('open-datasets');
  const syncTabs = () => {
    const open = !drawer.classList.contains('hidden');
    if (open) { allTab.removeAttribute('aria-current'); datasetsTab.setAttribute('aria-current', 'page'); }
    else { allTab.setAttribute('aria-current', 'page'); datasetsTab.removeAttribute('aria-current'); }
  };
  new MutationObserver(syncTabs).observe(drawer, { attributes: true, attributeFilter: ['class'] });
  syncTabs();
}

// Command palette: library actions (Ctrl/Cmd+K).
if (window.OF && window.OF.ui && window.OF.ui.palette) {
  window.OF.ui.palette.register({
    group: 'Library actions',
    items: () => [
      { label: 'New collection', icon: 'bi-plus-circle', run: () => { window.location.assign('/collections/new'); } },
      { label: 'Datasets', icon: 'bi-collection', run: () => datasets.open() },
      { label: 'Focus search', icon: 'bi-search', run: () => { const input = $('search-text'); input.focus(); input.select(); } },
    ],
  });
}

/** Add / edit a collection: source settings, InPort lookup, and metadata. */
import { $, el, clear, notice, yearRange, safeLink } from './ui.js';
import { api } from './api.js';
import { watchJob } from './jobs.js';
import { loadModels, fillModelSelect } from './models.js';

const form = $('collection-form');
const sourceId = Number(form.dataset.sourceId) || null;
let storedInportUrl = '';

const fields = {
  label: $('meta-label'),
  year_start: $('meta-year-start'),
  year_end: $('meta-year-end'),
  survey: $('meta-survey'),
  region: $('meta-region'),
  contact: $('meta-contact'),
  description: $('meta-description'),
};

// ── Extra key/value fields ──────────────────────────────────────────────────

function addExtraRow(key = '', value = '') {
  const row = el('div', { class: 'extras-row' }, [
    el('input', { type: 'text', class: 'tag-input extra-key', placeholder: 'field (e.g. vessel)', maxlength: 100, value: key }),
    el('input', { type: 'text', class: 'tag-input grow extra-value', placeholder: 'value', maxlength: 2000, value: value }),
    el('button', {
      type: 'button', class: 'btn btn-ghost btn-sm', text: '×', title: 'Remove field',
      onclick: () => row.remove(),
    }),
  ]);
  $('extras-rows').append(row);
}

function readExtras() {
  const extras = {};
  for (const row of $('extras-rows').querySelectorAll('.extras-row')) {
    const key = row.querySelector('.extra-key').value.trim();
    if (key) extras[key] = row.querySelector('.extra-value').value.trim();
  }
  return extras;
}

$('extras-add').addEventListener('click', () => addExtraRow());

// ── InPort ──────────────────────────────────────────────────────────────────

function renderInportPreview(summary) {
  const box = clear($('inport-preview'));
  if (!summary || !summary.title) { box.classList.add('hidden'); return; }
  box.classList.remove('hidden');
  const years = yearRange({
    year_start: Math.min(...yearsOf(summary)), year_end: Math.max(...yearsOf(summary)),
  });
  box.append(
    el('div', { class: 'inport-title' }, [safeLink(summary.url, summary.title)]),
    el('div', { class: 'sub', text: [summary.short_name, summary.item_type, summary.status, years].filter(Boolean).join(' · ') }),
    summary.doi ? el('div', { class: 'sub' }, ['DOI: ', safeLink(summary.doi, summary.doi)]) : null,
    summary.keywords?.length
      ? el('div', { class: 'chip-row' }, summary.keywords.slice(0, 16).map(k => el('span', { class: 'chip', text: k })))
      : null,
  );
}

function yearsOf(summary) {
  const found = [];
  for (const frame of summary.time_frames || []) {
    for (const match of `${frame.start} ${frame.end}`.matchAll(/\b(1[89]\d\d|2[01]\d\d)\b/g)) found.push(Number(match[1]));
  }
  return found.length ? found : [0];
}

/** Fill only the fields the user has left empty. */
function applySuggestions(suggested) {
  let filled = 0;
  for (const [key, input] of Object.entries(fields)) {
    const value = suggested[key];
    if (value === null || value === undefined || value === '') continue;
    if (input.value.trim()) continue;
    input.value = value;
    filled += 1;
  }
  return filled;
}

$('inport-lookup').addEventListener('click', async () => {
  const url = $('inport-url').value.trim();
  if (!url) { notice('Paste an InPort item URL first.'); return; }
  const button = $('inport-lookup');
  button.disabled = true;
  button.textContent = 'Looking up…';
  try {
    const { summary, suggested } = await api.inportLookup(url);
    $('inport-url').value = summary.url;
    renderInportPreview(summary);
    const filled = applySuggestions(suggested);
    notice(filled ? `Filled ${filled} empty field(s) from InPort.` : 'InPort record found; your fields were left as they are.');
  } catch (error) {
    notice(error.message, 'sticky');
  } finally {
    button.disabled = false;
    button.textContent = 'Lookup';
  }
});

// ── Load / save ─────────────────────────────────────────────────────────────

function readMeta() {
  const year = input => (input.value.trim() ? Number(input.value) : null);
  return {
    label: fields.label.value.trim(),
    year_start: year(fields.year_start),
    year_end: year(fields.year_end),
    survey: fields.survey.value.trim(),
    region: fields.region.value.trim(),
    contact: fields.contact.value.trim(),
    description: fields.description.value.trim(),
    extras: readExtras(),
    inport_url: $('inport-url').value.trim(),
  };
}

async function loadExisting(models) {
  const source = await api.source(sourceId);
  $('source-root').value = source.root;
  $('source-root').readOnly = true;
  $('source-pattern').value = source.tag_pattern || '';
  fillModelSelect($('source-model'), models, source.model_ref, { collectionWide: true });
  $('index-options').classList.add('hidden');
  for (const [key, input] of Object.entries(fields)) input.value = source[key] ?? '';
  $('inport-url').value = source.inport_url || '';
  storedInportUrl = source.inport_url || '';
  for (const [key, value] of Object.entries(source.extras || {})) addExtraRow(key, value);
  renderInportPreview(source.inport);
}

async function save(event) {
  event.preventDefault();
  const root = $('source-root').value.trim();
  if (!root) { notice('Enter a gs:// URI or a local directory first.'); return; }
  const meta = readMeta();
  if (meta.year_start && meta.year_end && meta.year_start > meta.year_end) {
    notice('"Year from" is after "Year to".');
    return;
  }

  const submit = $('form-submit');
  submit.disabled = true;
  try {
    const { id } = await api.addSource({
      root,
      label: meta.label,
      tag_pattern: $('source-pattern').value.trim(),
      model_ref: $('source-model').value,
      meta,
    });

    // The stored InPort summary is always fetched server-side, never taken from the form.
    if (meta.inport_url && (meta.inport_url !== storedInportUrl || !sourceId)) {
      try {
        await api.inportRefresh(id);
      } catch (error) {
        notice(`Saved, but InPort could not be read: ${error.message}`, 'sticky');
      }
    }

    if (sourceId) {
      window.location.assign('/collections');
      return;
    }
    const job = await api.index({
      root,
      force: $('index-force').checked,
      limit: Number($('index-limit').value) || 0,
    });
    notice('Collection saved — indexing has started.');
    watchJob(job, { onDone: () => window.location.assign('/collections') });
  } catch (error) {
    notice(error.message, 'sticky');
    submit.disabled = false;
  }
}

form.addEventListener('submit', save);
$('notice-close').addEventListener('click', () => $('notice-bar').classList.add('hidden'));

(async () => {
  const models = await loadModels();
  if (sourceId) {
    try {
      await loadExisting(models);
    } catch (error) {
      notice(error.message, 'sticky');
    }
    return;
  }
  fillModelSelect($('source-model'), models, '', { collectionWide: true });
  try {
    const status = await api.status();
    if (status.config.default_source) $('source-root').value = status.config.default_source;
  } catch { /* the form still works without a default */ }
})();

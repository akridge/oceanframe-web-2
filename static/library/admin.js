/** My collections page: list, scan with a model, re-index, embed, and watch jobs. */
import { $, el, clear, count, when, notice, yearRange, safeLink, renderStatusChips, icon } from './ui.js';
import { api } from './api.js';
import { watchJob } from './jobs.js';
import { loadModels, fillModelSelect } from './models.js';

function createCollectionsPage() {
  let models = [];

  async function refresh() {
    try {
      render(await api.status());
    } catch (error) {
      notice(error.message, 'sticky');
    }
  }
  const onRefresh = refresh;

  // ── Rendering ─────────────────────────────────────────────────────────────

  function render(status) {
    renderStatusChips(status);
    renderSummary(status);
    const sources = clear($('source-list'));
    if (!status.sources.length) {
      sources.append(el('div', { class: 'empty-collections' }, [
        icon('layers', 'icon icon-xl'),
        el('div', { class: 'collection-name', text: 'No collections yet' }),
        el('p', { class: 'card-subtitle', text: 'Point OceanFrame at a GCS prefix or a local folder to start. The form is pre-filled with NOAA open data.' }),
        el('a', { href: '/collections/new', class: 'btn btn-primary', text: '+ Add your first collection' }),
      ]));
    }
    for (const source of status.sources) sources.append(collectionRow(source));

    if (status.sources.length > 1) {
      sources.append(el('div', { class: 'scan-all-row' }, [
        el('button', {
          type: 'button', class: 'btn btn-secondary btn-sm', text: 'Scan every collection',
          title: 'Run each collection\'s own model over it, one after another',
          onclick: () => scanAll(status.sources),
        }),
        el('span', { class: 'sub', text: 'each with its own model' }),
      ]));
    }

    const embedder = status.embedder || {};
    $('embed-detail').textContent =
      `${embedder.backend || 'not loaded'} · ${embedder.dim || 0}d · ` +
      `${count(status.stats.embedded)} of ${count(status.stats.ok)} assets embedded. ${embedder.detail || ''}`;

    const modelStatus = clear($('model-status'));
    for (const annotator of status.annotators) {
      const state = annotator.loading ? 'loading' : annotator.available ? 'ready' : 'unavailable';
      const tone = annotator.loading ? '' : annotator.available ? 'good' : 'bad';
      modelStatus.append(el('div', { class: `list-row ${tone}` }, [
        el('div', { class: 'grow' }, [
          el('div', { text: `${annotator.name} — ${state}` }),
          el('div', { class: 'sub', text: annotator.detail }),
        ]),
      ]));
    }

    const jobList = clear($('job-list'));
    if (!status.jobs.length) jobList.append(el('div', { class: 'muted-row', text: 'Nothing has run yet.' }));
    for (const job of status.jobs) {
      jobList.append(el('div', { class: `list-row ${job.status === 'error' ? 'bad' : ''}` }, [
        el('div', { class: 'grow' }, [
          el('div', { text: `${job.kind} · ${job.status}` }),
          el('div', { class: 'sub', text: `${count(job.done)}/${count(job.total)} · ${job.elapsed}s · ${job.message || ''}` }),
        ]),
      ]));
    }
  }

  function renderSummary(status) {
    const box = clear($('collection-summary'));
    const sources = status.sources;
    const linked = sources.filter(s => s.inport_url).length;
    const stat = (name, value, label) => el('div', { class: 'summary-stat' }, [
      icon(name, 'icon summary-icon'),
      el('div', {}, [el('b', { text: value }), el('span', { text: label })]),
    ]);
    box.append(
      stat('layers', count(sources.length), 'collections'),
      stat('image', count(sources.reduce((n, s) => n + s.assets, 0)), 'images'),
      stat('target', count(sources.reduce((n, s) => n + s.detections, 0)), 'detections'),
      stat('record', `${linked}/${sources.length}`, 'linked to InPort'),
    );
  }

  function collectionRow(source) {
    const modelSelect = el('select', { class: 'select-sm' });
    fillModelSelect(modelSelect, models, source.model_ref, { collectionWide: true });
    // Remember the choice immediately, so "Scan" and any later run agree.
    modelSelect.addEventListener('change', () => {
      api.addSource({ root: source.root, model_ref: modelSelect.value })
        .catch(error => notice(error.message, 'sticky'));
    });

    const isCloud = source.kind === 'gcs';
    const metaChip = (name, text, title) => (text
      ? el('span', { class: 'meta-chip', title }, [icon(name), text]) : null);
    const metaChips = [
      metaChip('calendar', yearRange(source), 'Years'),
      metaChip('flag', source.survey, 'Survey / cruise / project'),
      metaChip('pin', source.region, 'Region'),
      metaChip('user', source.contact, 'Contact'),
    ].filter(Boolean);
    const stat = (name, value, label) => el('div', { class: 'coll-stat' }, [
      icon(name), el('b', { text: value }), el('span', { text: label }),
    ]);

    return el('article', { class: 'coll-card' }, [
      el('div', { class: 'coll-head' }, [
        el('div', { class: `coll-kind ${isCloud ? 'cloud' : 'local'}`, title: isCloud ? 'Google Cloud Storage' : 'Local folder' }, [
          icon(isCloud ? 'cloud' : 'folder', 'icon icon-lg'),
        ]),
        el('div', { class: 'grow' }, [
          el('h3', { class: 'coll-title', text: source.label || source.root }),
          el('div', { class: 'coll-root' }, [
            el('span', { class: 'coll-kind-label', text: isCloud ? 'GCS' : 'Local' }),
            el('span', { class: 'mono', text: source.root }),
          ]),
        ]),
        source.inport_url
          ? el('a', {
            class: 'inport-pill', href: source.inport_url, target: '_blank', rel: 'noopener noreferrer',
            title: source.inport_title || 'NOAA InPort metadata record',
          }, [icon('record'), el('span', { text: 'InPort' }), el('span', { class: 'inport-id', text: `#${source.inport_url.split('/').pop()}` })])
          : el('a', { class: 'inport-pill missing', href: `/collections/${source.id}/edit`, title: 'Link an InPort record' }, [
            icon('record'), el('span', { text: 'Add InPort' }),
          ]),
      ]),
      source.inport_title ? el('div', { class: 'coll-inport-title' }, [safeLink(source.inport_url, source.inport_title)]) : null,
      metaChips.length ? el('div', { class: 'meta-chips' }, metaChips) : null,
      source.description ? el('p', { class: 'coll-desc', text: source.description }) : null,
      el('div', { class: 'coll-stats' }, [
        stat('image', count(source.assets), 'images'),
        stat('target', count(source.detections), 'detections'),
        stat('clock', source.scanned_at ? when(source.scanned_at) : 'never', 'scanned'),
        source.tag_pattern ? stat('tag', 'path tags', source.tag_pattern) : null,
      ]),
      el('div', { class: 'coll-foot' }, [
        el('label', { class: 'coll-model', title: 'Model used when scanning this collection' }, [icon('cpu'), modelSelect]),
        el('div', { class: 'collection-actions' }, [
        el('button', {
          type: 'button', class: 'btn btn-primary btn-sm', text: 'Scan',
          title: 'Run the selected model over every image in this collection',
          onclick: () => scan(source, modelSelect.value),
        }),
        el('a', {
          class: 'btn btn-secondary btn-sm', text: 'Edit', href: `/collections/${source.id}/edit`,
        }),
        el('button', {
          type: 'button', class: 'btn btn-ghost btn-sm', text: 'Re-index',
          onclick: () => runIndex({ root: source.root }),
        }),
        source.inport_url ? el('button', {
          type: 'button', class: 'btn btn-ghost btn-sm', text: 'Refresh InPort',
          onclick: () => api.inportRefresh(source.id)
            .then(() => { notice('InPort record refreshed.'); onRefresh(); })
            .catch(error => notice(error.message, 'sticky')),
        }) : null,
        el('button', {
          type: 'button', class: 'btn btn-ghost btn-sm btn-danger-text', text: 'Forget',
          onclick: async () => {
            const sure = await window.OF.ui.confirm({
              title: 'Forget this collection?',
              message: `Remove "${source.label || source.root}" from the catalog?\n\nThe bucket objects are not touched.`,
              confirmLabel: 'Forget', danger: true,
            });
            if (!sure) return;
            await api.removeSource(source.id);
            onRefresh();
          },
        }),
        ]),
      ]),
    ]);
  }

  // ── Actions ───────────────────────────────────────────────────────────────

  /** Run one collection's model over every image in it. */
  function scan(source, modelRef) {
    return api.annotate({ annotator: 'yolo', source_id: source.id, model: modelRef })
      .then(job => new Promise(resolve => watchJob(job, {
        onDone: result => { onRefresh(); resolve(result); },
      })))
      .catch(error => { notice(error.message, 'sticky'); });
  }

  /**
   * Scan every collection with the model it is configured for, one after the
   * other — the job runner only allows one annotation at a time, so they have
   * to be chained rather than fired together.
   */
  async function scanAll(sources) {
    const targets = sources.filter(s => s.assets > 0);
    if (!targets.length) { notice('No collections to scan yet.'); return; }
    const sure = await window.OF.ui.confirm({
      title: 'Scan every collection?',
      message: `Scan ${targets.length} collection(s) with their configured models?\n\n` +
               'This runs one after another and can take a while on large collections.',
      confirmLabel: 'Scan',
    });
    if (!sure) return;
    for (const source of targets) {
      notice(`Scanning “${source.label || source.root}”…`);
      await scan(source, source.model_ref || '');
    }
    notice('Finished scanning every collection.');
    onRefresh();
  }

  function runIndex(body) {
    api.index(body)
      .then(job => watchJob(job, { onDone: onRefresh }))
      .catch(error => notice(error.message, 'sticky'));
  }

  $('embed-missing').addEventListener('click', () => {
    api.embed(false).then(job => watchJob(job, { onDone: onRefresh })).catch(e => notice(e.message, 'sticky'));
  });
  $('embed-rebuild').addEventListener('click', async () => {
    const sure = await window.OF.ui.confirm({
      title: 'Rebuild all vectors?',
      message: 'Rebuild every vector? Thumbnails, quality scores and tags are kept.',
      confirmLabel: 'Rebuild',
    });
    if (!sure) return;
    api.embed(true).then(job => watchJob(job, { onDone: onRefresh })).catch(e => notice(e.message, 'sticky'));
  });
  $('notice-close').addEventListener('click', () => $('notice-bar').classList.add('hidden'));

  return {
    async start() {
      models = await loadModels();
      await refresh();
    },
  };
}

createCollectionsPage().start();

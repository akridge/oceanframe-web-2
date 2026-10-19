/** Collection metadata on the main page: the (i) popup and the banner above the grid. */
import { $, el, clear, count, bindModal, notice, yearRange, safeLink } from './ui.js';
import { api } from './api.js';

export function createCollectionInfo() {
  const modal = bindModal('collection-info');

  function fact(label, value) {
    if (value === null || value === undefined || value === '') return [];
    return [el('dt', { text: label }), el('dd', {}, [value])];
  }

  function renderBody(source) {
    const box = clear($('collection-info-body'));
    const inport = source.inport || {};
    const extras = Object.entries(source.extras || {});

    box.append(...[
      el('h3', { class: 'detail-title', text: source.label || source.root }),
      el('p', { class: 'detail-uri mono', text: `${source.kind} · ${source.root}` }),
      el('dl', { class: 'fact-grid info-grid' }, [
        ...fact('Years', yearRange(source)),
        ...fact('Survey', source.survey),
        ...fact('Region', source.region),
        ...fact('Contact', source.contact),
        ...fact('Images', count(source.assets)),
        ...fact('Detections', count(source.detections)),
        ...extras.flatMap(([key, value]) => fact(key, value)),
      ]),
      source.description ? el('p', { class: 'info-description', text: source.description }) : null,
    ].filter(Boolean));

    if (source.inport_url) {
      box.append(el('h4', { class: 'detail-h4' }, [el('span', { class: 'inport-badge', text: 'InPort' }), ' metadata record']));
      box.append(el('div', { class: 'inport-title' }, [safeLink(source.inport_url, inport.title || source.inport_url)]));
      if (inport.title) {
        box.append(...[
          el('div', { class: 'sub', text: [inport.short_name, inport.item_type, inport.status].filter(Boolean).join(' · ') }),
          el('dl', { class: 'fact-grid info-grid' }, [
            ...fact('DOI', inport.doi ? safeLink(inport.doi, inport.doi) : ''),
            ...(inport.time_frames || []).flatMap(frame =>
              fact('Time frame', [frame.start, frame.end].filter(Boolean).join(' – '))),
            ...fact('Bounds', inport.bbox
              ? `W ${inport.bbox.west}, E ${inport.bbox.east}, N ${inport.bbox.north}, S ${inport.bbox.south}` : ''),
            ...(inport.contacts || []).filter(c => c.role).slice(0, 4).flatMap(c =>
              fact(c.role, c.email ? `${c.name} <${c.email}>` : c.name)),
          ]),
          inport.keywords?.length
            ? el('div', { class: 'chip-row' }, inport.keywords.slice(0, 24).map(k => el('span', { class: 'chip', text: k })))
            : null,
          inport.abstract ? el('details', { class: 'info-abstract' }, [
            el('summary', { text: 'Abstract' }), el('p', { text: inport.abstract }),
          ]) : null,
          inport.use_constraints ? el('details', { class: 'info-abstract' }, [
            el('summary', { text: 'Use constraints / citation' }), el('p', { text: inport.use_constraints }),
          ]) : null,
          (inport.urls || []).length ? el('ul', { class: 'info-links' },
            inport.urls.map(link => el('li', {}, [safeLink(link.url, link.name)]))) : null,
        ].filter(Boolean));
      } else {
        box.append(el('div', { class: 'muted-row', text: 'Not fetched yet — use “Refresh InPort” on My collections.' }));
      }
    }

    box.append(el('div', { class: 'detail-actions' }, [
      el('a', { class: 'btn btn-secondary btn-sm', href: `/collections/${source.id}/edit`, text: 'Edit metadata' }),
    ]));
  }

  async function open(sourceId) {
    try {
      renderBody(await api.source(sourceId));
      modal.open();
    } catch (error) {
      notice(error.message, 'sticky');
    }
  }

  /** Compact summary shown above the grid while exactly one collection is in scope. */
  function renderBanner(source) {
    const banner = $('collection-banner');
    clear(banner);
    if (!source) { banner.classList.add('hidden'); return; }
    const bits = [yearRange(source), source.survey, source.region].filter(Boolean).join(' · ');
    // DOM append() would print a null child as the text "null", so absent parts are dropped first.
    banner.append(...[
      el('div', { class: 'grow' }, [
        el('div', { class: 'collection-name', text: source.label || source.root }),
        bits ? el('div', { class: 'collection-meta', text: bits }) : null,
      ]),
      source.inport_url
        ? el('span', { class: 'banner-link' }, [el('span', { class: 'inport-badge', text: 'InPort' }), ' ',
            safeLink(source.inport_url, 'record')])
        : null,
      el('button', { type: 'button', class: 'btn btn-ghost btn-sm', text: 'Details', onclick: () => open(source.id) }),
    ].filter(Boolean));
    banner.classList.remove('hidden');
  }

  return { open, renderBanner };
}

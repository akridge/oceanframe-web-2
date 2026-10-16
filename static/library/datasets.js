/** Dataset drawer: list, split summary, export. */
import { $, el, clear, count, when, bytes, bindModal, notice } from './ui.js';
import { api } from './api.js';
import { watchJob } from './jobs.js';
import { state, setQuery } from './state.js';

const KINDS = [
  ['yolo-detect',   'YOLO detect'],
  ['yolo-seg',      'YOLO segment'],
  ['yolo-classify', 'YOLO classify (from class: tags)'],
  ['coco',          'COCO json'],
  ['csv',           'CSV manifest'],
];

export function createDatasets() {
  const modal = bindModal('datasets');

  async function refresh() {
    let data;
    let exportsData;
    try {
      [data, exportsData] = await Promise.all([api.datasets(), api.exports()]);
    } catch (error) {
      notice(error.message);
      return;
    }

    const list = clear($('dataset-list'));
    if (!data.datasets.length) {
      list.append(el('div', { class: 'muted-row', text: 'No datasets yet — select images and use “Make dataset”.' }));
    }

    for (const dataset of data.datasets) {
      const kindSelect = el('select', { class: 'select-sm' },
        KINDS.map(([value, label]) => el('option', { value, text: label })));
      const withImages = el('input', { type: 'checkbox', checked: true });

      list.append(el('div', { class: 'list-row', style: 'flex-wrap:wrap' }, [
        el('div', { class: 'grow' }, [
          el('div', { text: `${dataset.name} · ${count(dataset.size)} images` }),
          el('div', { class: 'sub', text: `${dataset.spec.split_mode || 'by_folder'} split · created ${when(dataset.created_at)}` +
            (dataset.labels.length ? ` · classes: ${dataset.labels.slice(0, 6).map(l => l.name).join(', ')}` : '') }),
        ]),
        el('div', { class: 'split-pills' },
          Object.entries(dataset.splits).sort().map(([name, n]) => el('span', { class: 'split-pill', text: `${name} ${n}` }))),
        el('div', { class: 'action-group' }, [
          kindSelect,
          el('label', { class: 'check-row inline' }, [withImages, ' images']),
          el('button', {
            type: 'button', class: 'btn btn-primary btn-sm', text: 'Export',
            onclick: () => {
              api.exportDataset(dataset.id, {
                kind: kindSelect.value, include_images: withImages.checked,
                conf: state.query.label_conf ?? 0.25,
              })
                .then(job => watchJob(job, { onDone: refresh }))
                .catch(error => notice(error.message, 'sticky'));
            },
          }),
          el('button', {
            type: 'button', class: 'btn btn-ghost btn-sm', text: 'Browse',
            onclick: () => { modal.close(); setQuery({ dataset_id: dataset.id, page: 0 }); },
          }),
          el('button', {
            type: 'button', class: 'btn btn-ghost btn-sm', text: 'Delete',
            onclick: async () => {
              const sure = await window.OF.ui.confirm({
                title: 'Delete this dataset?',
                message: `Delete dataset “${dataset.name}”? The images are untouched.`,
                confirmLabel: 'Delete', danger: true,
              });
              if (!sure) return;
              await api.deleteDataset(dataset.id);
              refresh();
            },
          }),
        ]),
      ]));
    }

    const exportList = clear($('export-list'));
    if (!exportsData.exports.length) {
      exportList.append(el('div', { class: 'muted-row', text: 'No exports yet.' }));
    }
    for (const item of exportsData.exports) {
      exportList.append(el('div', { class: 'list-row' }, [
        el('div', { class: 'grow' }, [
          el('div', { text: item.file }),
          el('div', { class: 'sub', text: `${bytes(item.bytes)} · ${when(item.mtime)}` }),
        ]),
        el('a', { class: 'btn btn-secondary btn-sm', href: item.url, download: true, text: 'Download' }),
      ]));
    }
  }

  return {
    open() { modal.open(); refresh(); },
    refresh,
  };
}

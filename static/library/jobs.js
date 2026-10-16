/**
 * Job progress: subscribe to a job's SSE stream and drive the toast.
 *
 * The stream replays a snapshot first, so reconnecting mid-run picks up where
 * the tab left off instead of showing an empty bar.
 */
import { $ } from './ui.js';
import { api } from './api.js';

const toast = () => ({
  box:   $('job-toast'),
  title: $('job-toast-title'),
  fill:  $('job-toast-fill'),
  msg:   $('job-toast-msg'),
  cancel: $('job-cancel'),
});

let current = null;
let source = null;

const TITLES = {
  index: 'Indexing', embed: 'Embedding', annotate: 'Detecting', export: 'Exporting',
};

export function watchJob(job, { onDone, onEvent } = {}) {
  const ui = toast();
  current = job.id;
  if (source) source.close();

  ui.title.textContent = TITLES[job.kind] || job.kind;
  ui.fill.style.width = '0%';
  ui.msg.textContent = job.message || 'Starting…';
  ui.box.classList.remove('hidden');
  ui.cancel.onclick = () => api.cancelJob(job.id).catch(() => {});

  source = new EventSource(`/api/library/jobs/${job.id}/stream`);
  source.onmessage = event => {
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    if (onEvent) onEvent(data);

    if (typeof data.frac === 'number') ui.fill.style.width = `${Math.round(data.frac * 100)}%`;
    if (data.message) ui.msg.textContent = data.message;
    if (data.total) {
      ui.title.textContent =
        `${TITLES[job.kind] || job.kind} ${(data.done || 0).toLocaleString()}/${data.total.toLocaleString()}`;
    }

    if (['done', 'error', 'cancelled'].includes(data.type)) {
      source.close();
      source = null;
      if (data.type === 'done') ui.fill.style.width = '100%';
      ui.msg.textContent = summarise(data);
      setTimeout(() => { if (current === job.id) ui.box.classList.add('hidden'); }, 4500);
      if (onDone) onDone(data);
    }
  };
  source.onerror = () => {
    // The stream ends normally when the job finishes; poll once to find out
    // which of the two happened rather than showing a spurious error.
    if (!source) return;
    source.close();
    source = null;
    api.jobs().then(({ jobs }) => {
      const found = jobs.find(j => j.id === job.id);
      if (found && onDone) onDone({ type: found.status, ...found });
      ui.msg.textContent = found ? summarise(found) : 'Connection lost';
      setTimeout(() => ui.box.classList.add('hidden'), 4000);
    }).catch(() => ui.box.classList.add('hidden'));
  };
}

function summarise(data) {
  if (data.type === 'error' || data.status === 'error') return `Failed: ${data.message || 'unknown error'}`;
  if (data.type === 'cancelled' || data.status === 'cancelled') return 'Cancelled';
  const r = data.result || {};
  if (r.indexed !== undefined) {
    return `Indexed ${r.indexed.toLocaleString()}, skipped ${r.skipped.toLocaleString()}` +
           (r.failed ? `, ${r.failed} failed` : '') + (r.missing ? `, ${r.missing} now missing` : '');
  }
  if (r.embedded !== undefined) return `Embedded ${r.embedded.toLocaleString()} with ${r.embedder}`;
  if (r.detections !== undefined) return `${r.detections.toLocaleString()} detections on ${r.assets.toLocaleString()} images`;
  if (r.written !== undefined) return `Wrote ${r.written.toLocaleString()} items to ${r.file}`;
  return 'Done';
}

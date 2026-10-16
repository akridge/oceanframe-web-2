import { state } from './state.js';
import {
  downloadZipBtn,
  downloadCsvBtn,
  resSelect,
  qualitySlider,
  exifCheckbox,
} from './dom.js';
import { $ } from './utils.js';
import { authFetch } from '../auth/session.js';

function setButtonBusy(btn, isBusy, busyLabel = 'Working...') {
  if (isBusy) {
    if (!btn.dataset.baseHtml) btn.dataset.baseHtml = btn.innerHTML;
    btn.dataset.prevDisabled = String(btn.disabled);
    btn.classList.add('is-loading');
    btn.disabled = true;
    btn.innerHTML = `<span class="btn-spinner" aria-hidden="true"></span><span>${busyLabel}</span>`;
    return;
  }

  if (btn.dataset.baseHtml) btn.innerHTML = btn.dataset.baseHtml;
  btn.classList.remove('is-loading');
  btn.disabled = btn.dataset.prevDisabled === 'true';
}

export function initExportModule() {
  downloadZipBtn.addEventListener('click', async () => {
    if (!state.sessionId) return;
    const indices = [...state.keptIndices];
    if (!indices.length) return;

    const fmt = document.querySelector('input[name="fmt"]:checked').value;
    const quality = Number(qualitySlider.value);
    const maxEdge = resSelect.value ? Number(resSelect.value) : null;
    const exif = exifCheckbox.checked && fmt === 'jpeg';

    setButtonBusy(downloadZipBtn, true, 'Building ZIP...');
    $('export-progress').classList.remove('hidden');
    $('export-label').textContent = `Building ZIP for ${indices.length} frames…`;
    $('export-bar').style.width = '10%';

    try {
      const res = await authFetch(`/api/export/${state.sessionId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          frame_indices: indices,
          format: fmt,
          quality,
          max_long_edge: maxEdge,
          write_exif: exif,
          tags: state.tags,
        }),
      });

      if (!res.ok) {
        window.OF.ui.toast('Export error: ' + (await res.json()).detail, { kind: 'danger' });
        return;
      }

      $('export-bar').style.width = '90%';
      const blob = await res.blob();
      $('export-bar').style.width = '100%';

      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'oceanframe_frames.zip';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(a.href);
    } catch (err) {
      window.OF.ui.toast('Export failed: ' + err.message, { kind: 'danger' });
    } finally {
      setButtonBusy(downloadZipBtn, false);
      setTimeout(() => {
        $('export-progress').classList.add('hidden');
        $('export-bar').style.width = '0%';
      }, 2000);
    }
  });

  downloadCsvBtn.addEventListener('click', async () => {
    if (!state.sessionId) return;
    setButtonBusy(downloadCsvBtn, true, 'Preparing CSV...');
    try {
      const res = await authFetch(`/api/csv/${state.sessionId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          kept_indices: [...state.keptIndices],
          manual_excludes: [...state.manualExcludes],
          manual_includes: [...state.manualIncludes],
          tags: state.tags,
        }),
      });

      if (!res.ok) {
        window.OF.ui.toast('CSV export failed', { kind: 'danger' });
        return;
      }

      const blob = await res.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'oceanframe_frames.csv';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(a.href);
    } catch (err) {
      window.OF.ui.toast('CSV export failed: ' + err.message, { kind: 'danger' });
    } finally {
      setButtonBusy(downloadCsvBtn, false);
    }
  });
}

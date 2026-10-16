import { state, batch } from './state.js';
import {
  modeGroup,
  startBtn,
  paramsSec,
  framesSec,
  exportSec,
  blurSlider,
  blurVal,
  simSlider,
  simVal,
  brightMinSlider,
  brightMaxSlider,
  downloadCsvBtn,
  sessionSwitcher,
  sessionSelect,
  batchQueue,
  batchList,
} from './dom.js';
import { showSection, hideSection } from './utils.js';
import { authFetch } from '../auth/session.js';

export function createBatchModule({
  loadSession,
  startAnalysis,
  analysisDeps,
  refilter,
  buildHistograms,
  syncBrightLabel,
}) {
  function addFilesToQueue(files) {
    if (!files.length) return;

    for (const f of files) {
      batch.queue.push({
        file: f,
        name: f.name,
        sessionId: null,
        status: 'waiting',
        frameCount: 0,
        savedState: null,
      });
    }

    showSection(batchQueue);
    renderBatchList();
    updateSessionSwitcher();

    if (!state.isAnalyzing) processNextInQueue();
  }

  function renderBatchList() {
    batchList.innerHTML = '';
    for (let i = 0; i < batch.queue.length; i++) {
      const item = batch.queue[i];
      const row = document.createElement('div');
      row.className = 'batch-item' + (i === batch.active ? ' active' : '');
      row.innerHTML = `
        <span class="bi-name" title="${item.name}">${item.name}</span>
        ${item.frameCount ? `<span class="text-muted text-sm">${item.frameCount.toLocaleString()} frames</span>` : ''}
        <span class="bi-status s-${item.status}">${item.status}</span>
      `;
      if (item.status === 'done') {
        row.addEventListener('click', () => restoreSession(i));
      }
      batchList.appendChild(row);
    }
  }

  async function processNextInQueue() {
    const idx = batch.queue.findIndex(i => i.status === 'waiting');
    if (idx === -1) return;

    batch.active = idx;
    const item = batch.queue[idx];
    item.status = 'processing';
    renderBatchList();

    const fd = new FormData();
    fd.append('file', item.file);

    let res;
    let data;
    try {
      res = await authFetch('/api/upload', { method: 'POST', body: fd });
      data = await res.json();
    } catch {
      item.status = 'error';
      renderBatchList();
      processNextInQueue();
      return;
    }

    if (!res.ok) {
      item.status = 'error';
      renderBatchList();
      processNextInQueue();
      return;
    }

    item.sessionId = data.session_id;

    loadSession(data.session_id, 'video', data.meta);
    modeGroup.style.display = '';
    startBtn.disabled = false;

    const mode = document.querySelector('input[name="mode"]:checked').value;
    startAnalysis(data.session_id, mode, () => {
      item.status = 'done';
      item.frameCount = state.allFrames.length;
      item.savedState = captureSessionState();
      renderBatchList();
      updateSessionSwitcher();
      batch.active = -1;
      setTimeout(processNextInQueue, 600);
    }, analysisDeps);
  }

  function captureSessionState() {
    return {
      sessionId: state.sessionId,
      sourceType: state.sourceType,
      allFrames: [...state.allFrames],
      keptIndices: new Set(state.keptIndices),
      manualExcludes: new Set(state.manualExcludes),
      manualIncludes: new Set(state.manualIncludes),
      tags: { ...state.tags },
      blurThreshold: state.blurThreshold,
      simThreshold: state.simThreshold,
      brightMin: state.brightMin,
      brightMax: state.brightMax,
    };
  }

  function restoreSession(idx) {
    const saved = batch.queue[idx]?.savedState;
    if (!saved) return;

    Object.assign(state, saved);
    blurSlider.value = state.blurThreshold;
    blurVal.textContent = state.blurThreshold;
    simSlider.value = state.simThreshold;
    simVal.textContent = state.simThreshold;
    brightMinSlider.value = state.brightMin;
    brightMaxSlider.value = state.brightMax;

    syncBrightLabel();
    refilter(true);
    buildHistograms();
    showSection(paramsSec);
    showSection(framesSec);
    showSection(exportSec);
    downloadCsvBtn.disabled = false;
    renderBatchList();
  }

  function updateSessionSwitcher() {
    const done = batch.queue.filter(i => i.status === 'done');
    if (done.length < 2) {
      hideSection(sessionSwitcher);
      return;
    }

    showSection(sessionSwitcher);
    sessionSelect.innerHTML = done.map((item, i) =>
      `<option value="${i}">${item.name}</option>`
    ).join('');
  }

  function initBatchControls() {
    sessionSelect.addEventListener('change', () => {
      const done = batch.queue.filter(i => i.status === 'done');
      const idx = batch.queue.indexOf(done[Number(sessionSelect.value)]);
      if (idx >= 0) restoreSession(idx);
    });
  }

  return {
    addFilesToQueue,
    initBatchControls,
  };
}

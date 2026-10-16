import { state } from './state.js';
import {
  startBtn,
  cancelBtn,
  progressSec,
  progressBar,
  progressLbl,
  progressCnt,
  progressPct,
  progressTime,
  paramsSec,
  framesSec,
  exportSec,
  downloadCsvBtn,
} from './dom.js';
import { hideSection, showSection } from './utils.js';
import { authFetch } from '../auth/session.js';

export function initAnalysisModule({ refilter, buildHistograms, buildTimeline }) {
  startBtn.addEventListener('click', () => {
    if (!state.sessionId || state.isAnalyzing) return;
    const mode = document.querySelector('input[name="mode"]:checked').value;
    startAnalysis(state.sessionId, mode, null, { refilter, buildHistograms, buildTimeline });
  });

  cancelBtn.addEventListener('click', () => {
    if (state.sessionId) authFetch(`/api/cancel/${state.sessionId}`, { method: 'POST' });
  });
}

export function startAnalysis(sessionId, mode, onComplete, deps) {
  state.allFrames = [];
  state.keptIndices.clear();
  state.manualExcludes.clear();
  state.manualIncludes.clear();
  state.isAnalyzing = true;
  state.startTime = Date.now();

  startBtn.disabled = true;
  showSection(progressSec);
  hideSection(paramsSec);
  hideSection(framesSec);
  hideSection(exportSec);
  progressBar.style.width = '0%';
  progressLbl.textContent = 'Connecting…';
  progressLbl.style.color = '';
  progressCnt.textContent = '0 frames';
  progressPct.textContent = '0%';

  const url = `/api/stream/${sessionId}?mode=${mode}`;
  const es = new EventSource(url);

  es.onmessage = e => {
    const msg = JSON.parse(e.data);
    handleSSEEvent(msg, es, onComplete, deps);
  };

  es.onerror = () => {
    state.isAnalyzing = false;
    progressLbl.textContent = 'Connection error';
    es.close();
    startBtn.disabled = false;
  };
}

function handleSSEEvent(msg, es, onComplete, deps) {
  if (msg.type === 'frame') {
    state.allFrames.push({
      index: msg.index,
      timestamp_ms: msg.timestamp_ms,
      blur_score: msg.blur_score,
      phash_hex: msg.phash_hex,
      brightness: msg.brightness ?? 128,
      color_cast: msg.color_cast ?? 1.0,
    });

    if (msg.count % 5 === 0 || msg.frac >= 0.99) {
      const elapsed = (Date.now() - state.startTime) / 1000;
      const fps = elapsed > 0 ? (msg.count / elapsed).toFixed(1) : '…';
      const pct = Math.round(msg.frac * 100);
      progressBar.style.width = pct + '%';
      progressLbl.textContent = `Analysing — ${fps} frames/sec`;
      progressCnt.textContent = `${msg.count.toLocaleString()} frames`;
      progressPct.textContent = `${pct}%`;
      progressTime.textContent = `${elapsed.toFixed(0)}s elapsed`;
    }
    if (state.allFrames.length % 50 === 0) deps.refilter(false);
    return;
  }

  if (msg.type === 'complete') {
    es.close();
    state.isAnalyzing = false;
    progressBar.style.width = '100%';
    progressLbl.textContent = `Complete — ${msg.total.toLocaleString()} frames analysed`;
    startBtn.disabled = false;

    deps.refilter(true);
    deps.buildHistograms();
    deps.buildTimeline();
    showSection(paramsSec);
    showSection(framesSec);
    showSection(exportSec);
    downloadCsvBtn.disabled = false;
    setTimeout(() => hideSection(progressSec), 2000);
    if (onComplete) onComplete();
    return;
  }

  if (msg.type === 'cancelled') {
    es.close();
    state.isAnalyzing = false;
    progressLbl.textContent = 'Cancelled';
    startBtn.disabled = false;
    if (state.allFrames.length > 0) {
      deps.refilter(true);
      deps.buildHistograms();
      deps.buildTimeline();
      showSection(paramsSec);
      showSection(framesSec);
      showSection(exportSec);
      downloadCsvBtn.disabled = false;
    }
    return;
  }

  if (msg.type === 'error') {
    es.close();
    state.isAnalyzing = false;
    progressLbl.textContent = 'Error: ' + msg.message;
    progressLbl.style.color = 'var(--error)';
    startBtn.disabled = false;
  }
}

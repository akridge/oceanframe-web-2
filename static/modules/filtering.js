import { state } from './state.js';
import {
  blurSlider,
  blurVal,
  simSlider,
  simVal,
  brightMinSlider,
  brightMaxSlider,
  brightRangeVal,
  keptCountEl,
  zipFrameCount,
  downloadZipBtn,
  qualitySlider,
  qualityVal,
} from './dom.js';
import { $, hammingHex } from './utils.js';

export function createFilteringModule({ drawTimeline, renderFrameGrid }) {
  function refilter(renderGrid = true) {
    const bt = state.blurThreshold;
    const st = state.simThreshold;
    const bMin = state.brightMin;
    const bMax = state.brightMax;

    const passing = state.allFrames.filter(f =>
      f.blur_score >= bt &&
      f.brightness >= bMin &&
      f.brightness <= bMax
    );

    const kept = [];
    let lastHash = null;
    for (const f of passing) {
      if (lastHash === null || hammingHex(lastHash, f.phash_hex) >= st) {
        kept.push(f);
        lastHash = f.phash_hex;
      }
    }

    const finalSet = new Set(kept.map(f => f.index));
    for (const idx of state.manualExcludes) finalSet.delete(idx);
    for (const idx of state.manualIncludes) finalSet.add(idx);
    state.keptIndices = finalSet;

    const count = finalSet.size;
    keptCountEl.textContent = count.toLocaleString();
    zipFrameCount.textContent = count.toLocaleString();
    downloadZipBtn.disabled = count === 0;

    drawTimeline();
    if (renderGrid) renderFrameGrid();
  }

  return { refilter };
}

export function syncBrightLabel() {
  brightRangeVal.textContent = `${state.brightMin}–${state.brightMax}`;
}

export function initFilterControls({
  refilter,
  updateBlurLine,
  updateSimLine,
  updateBrightLines,
}) {
  blurSlider.addEventListener('input', () => {
    state.blurThreshold = Number(blurSlider.value);
    blurVal.textContent = blurSlider.value;
    refilter();
    updateBlurLine();
  });

  simSlider.addEventListener('input', () => {
    state.simThreshold = Number(simSlider.value);
    simVal.textContent = simSlider.value;
    refilter();
    updateSimLine();
  });

  brightMinSlider.addEventListener('input', () => {
    state.brightMin = Math.min(Number(brightMinSlider.value), state.brightMax - 1);
    brightMinSlider.value = state.brightMin;
    syncBrightLabel();
    refilter();
    updateBrightLines();
  });

  brightMaxSlider.addEventListener('input', () => {
    state.brightMax = Math.max(Number(brightMaxSlider.value), state.brightMin + 1);
    brightMaxSlider.value = state.brightMax;
    syncBrightLabel();
    refilter();
    updateBrightLines();
  });

  qualitySlider.addEventListener('input', () => {
    qualityVal.textContent = qualitySlider.value;
  });

  document.querySelectorAll('input[name="fmt"]').forEach(r => {
    r.addEventListener('change', () => {
      $('quality-row').style.display = r.value === 'png' ? 'none' : '';
    });
  });
}

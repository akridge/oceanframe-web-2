import { state } from './state.js';
import {
  presetSelect,
  presetNameInput,
  presetSaveBtn,
  presetDeleteBtn,
  blurSlider,
  blurVal,
  simSlider,
  simVal,
  brightMinSlider,
  brightMaxSlider,
} from './dom.js';

const PRESET_KEY = 'oceanframe_presets';

function loadPresets() {
  try {
    return JSON.parse(localStorage.getItem(PRESET_KEY) || '{}');
  } catch {
    return {};
  }
}

function savePresets(presets) {
  localStorage.setItem(PRESET_KEY, JSON.stringify(presets));
}

function refreshPresetUI() {
  const presets = loadPresets();
  const keys = Object.keys(presets);
  presetSelect.innerHTML = '<option value="">— load a preset —</option>' +
    keys.map(k => `<option value="${k}">${k}</option>`).join('');
  presetDeleteBtn.disabled = true;
}

export function initPresetModule({ refilter, syncBrightLabel, updateChartLines }) {
  presetSaveBtn.addEventListener('click', () => {
    const name = presetNameInput.value.trim();
    if (!name) {
      presetNameInput.focus();
      return;
    }

    const presets = loadPresets();
    presets[name] = {
      blurThreshold: state.blurThreshold,
      simThreshold: state.simThreshold,
      brightMin: state.brightMin,
      brightMax: state.brightMax,
    };
    savePresets(presets);
    refreshPresetUI();
    presetNameInput.value = '';
  });

  presetSelect.addEventListener('change', () => {
    const name = presetSelect.value;
    presetDeleteBtn.disabled = !name;
    if (!name) return;

    const preset = loadPresets()[name];
    if (!preset) return;

    blurSlider.value = preset.blurThreshold;
    simSlider.value = preset.simThreshold;
    brightMinSlider.value = preset.brightMin ?? 0;
    brightMaxSlider.value = preset.brightMax ?? 255;

    state.blurThreshold = preset.blurThreshold;
    state.simThreshold = preset.simThreshold;
    state.brightMin = preset.brightMin ?? 0;
    state.brightMax = preset.brightMax ?? 255;

    blurVal.textContent = preset.blurThreshold;
    simVal.textContent = preset.simThreshold;

    syncBrightLabel();
    refilter();
    updateChartLines();
  });

  presetDeleteBtn.addEventListener('click', () => {
    const name = presetSelect.value;
    if (!name) return;

    const presets = loadPresets();
    delete presets[name];
    savePresets(presets);
    refreshPresetUI();
  });

  refreshPresetUI();
}

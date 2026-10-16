import { state } from './state.js';
import {
  $,
  fmtTime,
  fmtScore,
  hammingHex,
  niceMax,
  showSection,
} from './utils.js';
import {
  blurSlider,
  blurVal,
  simSlider,
  simVal,
  brightMinSlider,
  brightMaxSlider,
} from './dom.js';

/** A design token's current value (theme aware). */
function themeColor(name) {
  return window.OF.ui.theme.token(name) || 'gray';
}

/** "#rrggbb" or "#rgb" with an alpha; anything else is returned unchanged. */
function alpha(color, a) {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color);
  if (!m) return color;
  const h = m[1].length === 3 ? m[1].replace(/./g, ch => ch + ch) : m[1];
  const n = parseInt(h, 16);
  return `rgba(${n >> 16 & 255},${n >> 8 & 255},${n & 255},${a})`;
}

export function createChartsModule({ Chart, refilter, syncBrightLabel, openLightbox }) {
  initTimelineClick(openLightbox);

  // A theme switch recolours every live chart and the timeline.
  window.OF.ui.theme.onChange(() => {
    for (const chart of [state.blurChart, state.simChart, state.brightChart]) {
      if (!chart) continue;
      for (const axis of ['x', 'y']) {
        chart.options.scales[axis].grid.color = themeColor('--line');
        chart.options.scales[axis].ticks.color = themeColor('--text-3');
      }
    }
    updateBlurLine();
    updateSimLine();
    updateBrightLines();
    drawTimeline();
  });

  function buildHistograms() {
    buildBlurChart();
    buildSimChart();
    buildBrightChart();
  }

  function buildBlurChart() {
    const scores = state.allFrames.map(f => f.blur_score);
    if (!scores.length) return;

    const BINS = 50;
    const maxVal = Math.max(...scores);
    const sliderMax = niceMax(maxVal);
    blurSlider.max = sliderMax;
    blurSlider.step = sliderMax >= 1000 ? 10 : sliderMax >= 100 ? 5 : 1;
    if (state.blurThreshold > sliderMax) {
      state.blurThreshold = 0;
      blurSlider.value = 0;
      blurVal.textContent = 0;
    }
    $('blur-slider-max').textContent = `${sliderMax} — strict`;

    const step = maxVal / BINS || 1;
    const edges = Array.from({ length: BINS }, (_, i) => +(i * step).toFixed(1));
    const counts = new Array(BINS).fill(0);
    for (const s of scores) counts[Math.min(Math.floor(s / step), BINS - 1)]++;
    state._blurEdges = edges;
    state._blurStep = step;

    if (state.blurChart) state.blurChart.destroy();
    state.blurChart = new Chart($('blur-chart').getContext('2d'), {
      type: 'bar',
      data: {
        labels: edges,
        datasets: [{
          data: counts,
          backgroundColor: blurColors(edges),
          borderWidth: 0,
          barPercentage: 1,
          categoryPercentage: 1,
        }],
      },
      options: chartOpts(
        items => `Sharpness ≥ ${items[0].label}`,
        i => `${i.raw} frames`,
        elements => {
          if (!elements.length) return;
          const v = Math.round(edges[elements[0].index]);
          blurSlider.value = v;
          state.blurThreshold = v;
          blurVal.textContent = v;
          refilter();
          updateBlurLine();
        }
      ),
    });
  }

  function blurColors(edges) {
    const c = themeColor('--accent');
    return edges.map(e => e >= state.blurThreshold ? alpha(c, 0.75) : alpha(c, 0.22));
  }

  function updateBlurLine() {
    if (!state.blurChart) return;
    state.blurChart.data.datasets[0].backgroundColor = blurColors(state._blurEdges);
    state.blurChart.update('none');
  }

  function buildSimChart() {
    const frames = state.allFrames;
    if (frames.length < 2) return;

    const counts = new Array(33).fill(0);
    for (let i = 0; i < frames.length - 1; i++) {
      counts[Math.min(hammingHex(frames[i].phash_hex, frames[i + 1].phash_hex), 32)]++;
    }
    const labels = Array.from({ length: 33 }, (_, i) => i);

    if (state.simChart) state.simChart.destroy();
    state.simChart = new Chart($('sim-chart').getContext('2d'), {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          data: counts,
          backgroundColor: simColors(labels),
          borderWidth: 0,
          barPercentage: 1,
          categoryPercentage: 1,
        }],
      },
      options: chartOpts(
        items => `Distance = ${items[0].label}`,
        i => `${i.raw} pairs`,
        elements => {
          if (!elements.length) return;
          const v = elements[0].index;
          simSlider.value = v;
          state.simThreshold = v;
          simVal.textContent = v;
          refilter();
          updateSimLine();
        }
      ),
    });
  }

  function simColors(labels) {
    const c = themeColor('--ok');
    return labels.map(l => l >= state.simThreshold ? alpha(c, 0.75) : alpha(c, 0.22));
  }

  function updateSimLine() {
    if (!state.simChart) return;
    state.simChart.data.datasets[0].backgroundColor = simColors(Array.from({ length: 33 }, (_, i) => i));
    state.simChart.update('none');
  }

  function buildBrightChart() {
    const scores = state.allFrames.map(f => f.brightness);
    if (!scores.length) return;

    const BINS = 32;
    const step = 256 / BINS;
    const edges = Array.from({ length: BINS }, (_, i) => Math.round(i * step));
    const counts = new Array(BINS).fill(0);
    for (const s of scores) counts[Math.min(Math.floor(s / step), BINS - 1)]++;

    if (state.brightChart) state.brightChart.destroy();
    state.brightChart = new Chart($('bright-chart').getContext('2d'), {
      type: 'bar',
      data: {
        labels: edges,
        datasets: [{
          data: counts,
          backgroundColor: brightColors(edges, step),
          borderWidth: 0,
          barPercentage: 1,
          categoryPercentage: 1,
        }],
      },
      options: chartOpts(
        items => `Brightness ~ ${items[0].label}`,
        i => `${i.raw} frames`,
        elements => {
          if (!elements.length) return;
          const v = edges[elements[0].index];
          const midMin = (state.brightMin + state.brightMax) / 2;
          if (v < midMin) {
            brightMinSlider.value = v;
            state.brightMin = v;
          } else {
            brightMaxSlider.value = v;
            state.brightMax = v;
          }
          syncBrightLabel();
          refilter();
          updateBrightLines();
        }
      ),
    });
  }

  function brightColors(edges, step) {
    const c = themeColor('--warn');
    return edges.map(e => (e + step >= state.brightMin && e <= state.brightMax)
      ? alpha(c, 0.75)
      : alpha(c, 0.2)
    );
  }

  function updateBrightLines() {
    if (!state.brightChart) return;
    const step = 256 / 32;
    const edges = Array.from({ length: 32 }, (_, i) => Math.round(i * step));
    state.brightChart.data.datasets[0].backgroundColor = brightColors(edges, step);
    state.brightChart.update('none');
  }

  function chartOpts(titleFn, labelFn, clickFn) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { title: titleFn, label: labelFn } } },
      scales: {
        x: { display: false, grid: { color: themeColor('--line') }, ticks: { color: themeColor('--text-3') } },
        y: { display: false, grid: { color: themeColor('--line') }, ticks: { color: themeColor('--text-3') } },
      },
      onClick(event, elements) { clickFn(elements); },
    };
  }

  function buildTimeline() {
    if (!state.allFrames.length) return;
    showSection($('timeline-wrap'));
    const dur = state.allFrames[state.allFrames.length - 1].timestamp_ms;
    $('tl-end').textContent = fmtTime(dur);
    drawTimeline();
  }

  function drawTimeline() {
    const canvas = $('timeline-canvas');
    if (!canvas || !state.allFrames.length) return;

    const W = canvas.offsetWidth || 800;
    const H = 28;
    canvas.width = W;
    canvas.height = H;

    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = themeColor('--line');
    ctx.fillRect(0, 0, W, H);

    const maxTs = state.allFrames[state.allFrames.length - 1].timestamp_ms || 1;

    for (const f of state.allFrames) {
      const x = Math.round((f.timestamp_ms / maxTs) * (W - 1));
      if (state.manualExcludes.has(f.index)) ctx.fillStyle = alpha(themeColor('--danger'), 0.85);
      else if (state.manualIncludes.has(f.index)) ctx.fillStyle = alpha(themeColor('--ok'), 0.9);
      else if (state.keptIndices.has(f.index)) ctx.fillStyle = alpha(themeColor('--accent'), 0.75);
      else ctx.fillStyle = alpha(themeColor('--accent'), 0.12);
      ctx.fillRect(x, 0, Math.max(1, (W / state.allFrames.length) * 0.8), H);
    }
  }

  return {
    buildHistograms,
    buildTimeline,
    drawTimeline,
    updateBlurLine,
    updateSimLine,
    updateBrightLines,
  };
}

function initTimelineClick(openLightbox) {
  $('timeline-canvas').addEventListener('click', e => {
    if (!state.allFrames.length) return;

    const rect = e.target.getBoundingClientRect();
    const frac = (e.clientX - rect.left) / rect.width;
    const maxTs = state.allFrames[state.allFrames.length - 1].timestamp_ms || 1;
    const targetTs = frac * maxTs;

    const nearest = state.allFrames.reduce((best, f) =>
      Math.abs(f.timestamp_ms - targetTs) < Math.abs(best.timestamp_ms - targetTs) ? f : best
    );

    openLightbox(nearest);
  });
}

import { state } from './state.js';
import {
  frameGrid,
  framesEmpty,
  sortSelect,
  sizeBtns,
} from './dom.js';
import {
  fmtTime,
  fmtScore,
  qualityClass,
} from './utils.js';

export function createFrameBrowserModule({ openLightbox }) {
  function keptFrames() {
    let frames = state.allFrames.filter(f => state.keptIndices.has(f.index));
    const sort = sortSelect.value;
    if (sort === 'blur_desc') frames.sort((a, b) => b.blur_score - a.blur_score);
    else if (sort === 'blur_asc') frames.sort((a, b) => a.blur_score - b.blur_score);
    else if (sort === 'bright_desc') frames.sort((a, b) => b.brightness - a.brightness);
    else if (sort === 'bright_asc') frames.sort((a, b) => a.brightness - b.brightness);
    return frames;
  }

  function renderFrameGrid() {
    if (frameGrid.classList.contains('grid-sq')) {
      renderSquaresGrid();
      return;
    }
    const frames = keptFrames();
    if (!frames.length) {
      frameGrid.innerHTML = '';
      framesEmpty.classList.remove('hidden');
      return;
    }
    framesEmpty.classList.add('hidden');
    frameGrid.innerHTML = '';
    for (const f of frames) frameGrid.appendChild(buildFrameCard(f));
  }

  function buildFrameCard(f) {
    const card = document.createElement('div');
    card.className = 'frame-card' + (state.manualExcludes.has(f.index) ? ' excluded' : '');
    card.dataset.index = f.index;

    const img = document.createElement('img');
    img.className = 'frame-thumb';
    img.src = `/api/thumb/${state.sessionId}/${f.index}`;
    img.alt = `Frame ${f.index}`;
    img.loading = 'lazy';
    img.decoding = 'async';
    card.appendChild(img);

    const info = document.createElement('div');
    info.className = 'frame-info';

    const ts = document.createElement('div');
    ts.className = 'frame-ts';
    ts.textContent = fmtTime(f.timestamp_ms);
    info.appendChild(ts);

    const row = document.createElement('div');
    row.className = 'frame-blur-row';
    const dot = document.createElement('span');
    dot.className = `quality-dot ${qualityClass(f.blur_score)}`;
    const val = document.createElement('span');
    val.className = 'frame-blur-val';
    val.textContent = fmtScore(f.blur_score);
    row.appendChild(dot);
    row.appendChild(val);
    info.appendChild(row);

    const frameTags = state.tags[String(f.index)];
    if (frameTags?.length) {
      const tagRow = document.createElement('div');
      tagRow.className = 'frame-tags';
      for (const t of frameTags) {
        const pill = document.createElement('span');
        pill.className = 'frame-tag-pill';
        pill.textContent = t;
        tagRow.appendChild(pill);
      }
      info.appendChild(tagRow);
    }

    card.appendChild(info);

    if (state.manualExcludes.has(f.index)) {
      const b = document.createElement('span');
      b.className = 'exclude-badge';
      b.textContent = 'excluded';
      card.appendChild(b);
    } else if (state.manualIncludes.has(f.index)) {
      const b = document.createElement('span');
      b.className = 'include-badge';
      b.textContent = 'included';
      card.appendChild(b);
    }

    card.addEventListener('click', () => openLightbox(f));
    return card;
  }

  function renderSquaresGrid() {
    framesEmpty.classList.add('hidden');
    frameGrid.innerHTML = '';
    if (!state.allFrames.length) return;

    const blurPass = new Set(state.allFrames.filter(f =>
      f.blur_score >= state.blurThreshold &&
      f.brightness >= state.brightMin &&
      f.brightness <= state.brightMax
    ).map(f => f.index));

    const legend = document.createElement('div');
    legend.className = 'sq-legend';
    legend.style.gridColumn = '1/-1';
    legend.innerHTML = `
      <span class="sq-legend-item"><span class="sq-dot sq-kept"></span>Kept</span>
      <span class="sq-legend-item"><span class="sq-dot sq-blur"></span>Too blurry / dark / bright</span>
      <span class="sq-legend-item"><span class="sq-dot sq-sim"></span>Duplicate</span>
      <span class="sq-legend-item"><span class="sq-dot sq-excluded"></span>Manually excluded</span>
      <span class="sq-legend-item"><span class="sq-dot sq-included"></span>Manually included</span>
    `;
    frameGrid.appendChild(legend);

    for (const f of state.allFrames) {
      let cls = 'frame-sq ';
      if (state.manualExcludes.has(f.index)) cls += 'sq-excluded';
      else if (state.manualIncludes.has(f.index)) cls += 'sq-included';
      else if (state.keptIndices.has(f.index)) cls += 'sq-kept';
      else if (!blurPass.has(f.index)) cls += 'sq-blur';
      else cls += 'sq-sim';

      const sq = document.createElement('div');
      sq.className = cls;
      sq.title = `#${f.index}  ${fmtTime(f.timestamp_ms)}  sharpness:${fmtScore(f.blur_score)}  brightness:${fmtScore(f.brightness)}`;
      sq.addEventListener('click', () => openLightbox(f));
      frameGrid.appendChild(sq);
    }
  }

  function initFrameBrowserControls() {
    sortSelect.addEventListener('change', renderFrameGrid);

    sizeBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        sizeBtns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        frameGrid.className = `frame-grid grid-${btn.dataset.size}`;
        const isSq = btn.dataset.size === 'sq';
        sortSelect.closest('.browser-controls').querySelector('label').style.display = isSq ? 'none' : '';
        sortSelect.style.display = isSq ? 'none' : '';
        renderFrameGrid();
      });
    });
  }

  return {
    keptFrames,
    renderFrameGrid,
    initFrameBrowserControls,
  };
}

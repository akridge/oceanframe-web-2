import { state } from './state.js';
import {
  lightbox,
  lightboxBg,
  lightboxImg,
  lightboxClose,
  lightboxPrev,
  lightboxNext,
  lbIndex,
  lbTs,
  lbBlur,
  lbBright,
  lbCast,
  lbHash,
  lbToggleBtn,
  tagInput,
  tagAddBtn,
  tagChips,
} from './dom.js';
import { fmtTime, fmtScore } from './utils.js';

export function createLightboxModule({ keptFrames, refilter, renderFrameGrid }) {
  let lightboxFrame = null;

  function openLightbox(f) {
    const frames = keptFrames();
    state.lightboxIndex = frames.findIndex(x => x.index === f.index);
    if (state.lightboxIndex < 0) state.lightboxIndex = 0;
    showLightboxFrame(f);
    lightbox.classList.remove('hidden');
    lightboxBg.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
  }

  function showLightboxFrame(f) {
    lightboxFrame = f;
    lightboxImg.src = `/api/thumb/${state.sessionId}/${f.index}`;
    lightboxImg.style.filter = 'blur(2px)';

    const hi = new Image();
    hi.onload = () => {
      if (lightboxFrame === f) {
        lightboxImg.src = hi.src;
        lightboxImg.style.filter = '';
      }
    };
    hi.src = `/api/frame/${state.sessionId}/${f.index}`;

    lbIndex.textContent = f.index.toLocaleString();
    lbTs.textContent = `${fmtTime(f.timestamp_ms)} (${f.timestamp_ms.toFixed(0)} ms)`;
    lbBlur.textContent = fmtScore(f.blur_score);
    lbBright.textContent = fmtScore(f.brightness) + ' / 255';
    lbHash.textContent = f.phash_hex;

    const cast = f.color_cast;
    lbCast.textContent = cast < 0.8 ? `${cast} (blue-heavy)` : cast > 1.2 ? `${cast} (red-heavy)` : `${cast} (neutral)`;

    lbToggleBtn.textContent = state.manualExcludes.has(f.index) ? 'Re-include this frame' : 'Exclude this frame';
    lbToggleBtn.onclick = () => {
      toggleExclude(f.index);
      showLightboxFrame(f);
    };

    renderTagChips(f.index);
  }

  function closeLightbox() {
    lightbox.classList.add('hidden');
    lightboxBg.classList.add('hidden');
    document.body.style.overflow = '';
    lightboxFrame = null;
  }

  function toggleExclude(idx) {
    if (state.manualExcludes.has(idx)) state.manualExcludes.delete(idx);
    else {
      state.manualExcludes.add(idx);
      state.manualIncludes.delete(idx);
    }
    refilter();
  }

  function renderTagChips(idx) {
    tagChips.innerHTML = '';
    const tags = state.tags[String(idx)] || [];
    for (const t of tags) {
      const chip = document.createElement('span');
      chip.className = 'tag-chip';
      chip.innerHTML = `${t}<button title="Remove" data-tag="${t}">×</button>`;
      chip.querySelector('button').addEventListener('click', () => removeTag(idx, t));
      tagChips.appendChild(chip);
    }
  }

  function addTag(idx, tag) {
    const normalized = tag.trim().toLowerCase();
    if (!normalized) return;

    const key = String(idx);
    if (!state.tags[key]) state.tags[key] = [];
    if (!state.tags[key].includes(normalized)) state.tags[key].push(normalized);
    renderTagChips(idx);
    renderFrameGrid();
  }

  function removeTag(idx, tag) {
    const key = String(idx);
    if (state.tags[key]) state.tags[key] = state.tags[key].filter(t => t !== tag);
    renderTagChips(idx);
    renderFrameGrid();
  }

  function initLightboxControls() {
    lightboxClose.addEventListener('click', closeLightbox);
    lightboxBg.addEventListener('click', closeLightbox);

    lightboxPrev.addEventListener('click', () => {
      const frames = keptFrames();
      if (!frames.length) return;
      state.lightboxIndex = (state.lightboxIndex - 1 + frames.length) % frames.length;
      showLightboxFrame(frames[state.lightboxIndex]);
    });

    lightboxNext.addEventListener('click', () => {
      const frames = keptFrames();
      if (!frames.length) return;
      state.lightboxIndex = (state.lightboxIndex + 1) % frames.length;
      showLightboxFrame(frames[state.lightboxIndex]);
    });

    document.addEventListener('keydown', e => {
      if (lightbox.classList.contains('hidden')) return;
      if (e.key === 'Escape') closeLightbox();
      if (e.key === 'ArrowLeft') lightboxPrev.click();
      if (e.key === 'ArrowRight') lightboxNext.click();
    });

    tagAddBtn.addEventListener('click', () => {
      if (lightboxFrame) {
        addTag(lightboxFrame.index, tagInput.value);
        tagInput.value = '';
      }
    });

    tagInput.addEventListener('keydown', e => {
      if (e.key === 'Enter' && lightboxFrame) {
        addTag(lightboxFrame.index, tagInput.value);
        tagInput.value = '';
      }
    });
  }

  return {
    openLightbox,
    initLightboxControls,
  };
}

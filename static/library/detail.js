/**
 * Asset detail modal: full image with detection overlay, the OceanFrame quality
 * breakdown, tags, and a strip of visually similar images.
 */
import { $, el, clear, bytes, band, bindModal, notice } from './ui.js';
import { api } from './api.js';
import { state, setQuery, clearVectorQuery } from './state.js';

const PALETTE = ['#00b4d8', '#facc15', '#f97316', '#a78bfa', '#34d399', '#f472b6'];

export function createDetail() {
  const modal = bindModal('detail');
  let currentId = null;

  async function open(assetId) {
    let asset;
    try {
      asset = await api.asset(assetId);
    } catch (error) {
      notice(error.message);
      return;
    }
    currentId = assetId;
    modal.open();

    $('detail-name').textContent = asset.name;
    $('detail-uri').textContent = asset.uri;
    // The preview, not the original: a 13 MB photogrammetry frame took 7s to
    // paint here. "Open full size" still serves the untouched source object.
    $('detail-img').src = asset.preview || asset.full;
    $('detail-img').alt = asset.name;
    $('detail-open').href = asset.full;

    renderQuality(asset);
    renderFacts(asset);
    renderTags(asset);
    renderDetections(asset);
    renderSimilar(asset);
  }

  function renderQuality(asset) {
    const box = clear($('detail-quality'));
    const parts = asset.quality_breakdown;
    box.append(el('div', { class: 'quality-head' }, [
      el('span', { text: 'OceanFrame quality' }),
      el('b', { class: band(asset.quality), text: Math.round(asset.quality) }),
    ]));
    for (const key of ['sharpness', 'exposure', 'contrast', 'colour']) {
      const value = parts[key] ?? 0;
      box.append(el('div', { class: 'qbar-row' }, [
        el('span', { text: `${key} ·${Math.round((parts.weights[key] || 0) * 100)}%` }),
        el('span', { class: 'qbar' }, [el('span', { style: `width:${value}%` })]),
        el('span', { class: 'mono', text: Math.round(value) }),
      ]));
    }
  }

  function renderFacts(asset) {
    const grid = clear($('detail-facts'));
    const facts = [
      ['Folder', asset.folder || '/'],
      ['Size', `${asset.width}×${asset.height} · ${bytes(asset.size)}`],
      ['Blur (Laplacian var.)', asset.blur],
      ['Brightness', asset.brightness],
      ['Contrast', asset.contrast],
      ['Colour cast R/B', asset.color_cast],
      ['pHash', asset.phash],
      ['Source', asset.source_root],
    ];
    for (const [key, value] of facts) {
      grid.append(el('dt', { text: key }));
      grid.append(el('dd', { text: String(value) }));
    }
  }

  function renderTags(asset) {
    const box = clear($('detail-tags'));
    if (!asset.tags || !asset.tags.length) {
      box.append(el('span', { class: 'muted-row', text: 'No tags' }));
      return;
    }
    for (const tag of asset.tags) {
      box.append(el('span', { class: 'chip' }, [
        el('button', { type: 'button', text: tag, title: 'Filter by this tag', style: 'border:0;background:none;cursor:pointer;font:inherit', onclick: () => {
          if (!state.query.tags.includes(tag)) state.query.tags.push(tag);
          modal.close();
          setQuery({ page: 0 });
        } }),
        el('button', {
          type: 'button', text: '×', title: 'Remove tag',
          onclick: async () => {
            await api.removeTags({ asset_ids: [asset.id], names: [tag] });
            open(asset.id);
          },
        }),
      ]));
    }
  }

  function renderDetections(asset) {
    const list = clear($('detail-dets'));
    const overlay = $('detail-overlay');
    const boxes = clear($('detail-boxes'));
    clear(overlay);

    if (!asset.detections.length) {
      list.append(el('span', { class: 'muted-row', text: 'None — run YOLO or SAM 3 on this image' }));
      return;
    }

    const colours = new Map();
    for (const det of asset.detections) {
      if (!colours.has(det.label)) colours.set(det.label, PALETTE[colours.size % PALETTE.length]);
      const colour = colours.get(det.label);

      // Boxes are normalised cx, cy, w, h.  They are drawn as positioned
      // elements rather than SVG shapes because the overlay is stretched to a
      // non-square box: an SVG stroke would come out thicker on one axis, and
      // a text label would come out visibly distorted.
      const [cx, cy, w, h] = det.box;
      const box = el('div', {
        class: 'det-box',
        style: `left:${(cx - w / 2) * 100}%;top:${(cy - h / 2) * 100}%;` +
               `width:${w * 100}%;height:${h * 100}%;` +
               `border-color:${colour};background:${colour}1f`,
      }, [
        el('span', { class: 'det-box-tag', style: `background:${colour}`,
                     text: `${det.label} ${det.conf.toFixed(2)}` }),
      ]);
      boxes.append(box);

      if (det.mask) drawMask(overlay, det.mask, colour);

      list.append(el('div', { class: 'det-row' }, [
        el('span', {}, [
          el('span', { class: 'det-swatch', style: `background:${colour}` }),
          det.label,
        ]),
        el('span', { class: 'conf', text: det.conf.toFixed(2) }),
      ]));
    }
  }

  /** Segmentation polygons are normalised too; fill only, so no stroke to skew. */
  function drawMask(overlay, polygons, colour) {
    for (const polygon of polygons) {
      if (!polygon || polygon.length < 6) continue;
      const points = [];
      for (let i = 0; i < polygon.length - 1; i += 2) {
        points.push(`${polygon[i] * 100},${polygon[i + 1] * 100}`);
      }
      const shape = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
      shape.setAttribute('points', points.join(' '));
      shape.setAttribute('fill', colour);
      shape.setAttribute('fill-opacity', '0.28');
      overlay.append(shape);
    }
  }

  function renderSimilar(asset) {
    const strip = clear($('detail-similar'));
    if (!asset.similar.length) {
      strip.append(el('span', { class: 'muted-row', text: 'No embedding for this asset yet' }));
      return;
    }
    for (const neighbour of asset.similar) {
      strip.append(el('img', {
        src: neighbour.thumb, alt: neighbour.name,
        title: `${neighbour.name} · ${neighbour.score?.toFixed(3) ?? ''}`,
        onclick: () => open(neighbour.id),
      }));
    }
  }

  $('detail-similar-search').addEventListener('click', () => {
    if (!currentId) return;
    modal.close();
    clearVectorQuery();
    setQuery({ similar_to: currentId, text: '', sort: 'relevance' });
  });

  return { open };
}

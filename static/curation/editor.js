// Editing points on the source image: drag to move, corner handle to resize,
// Remove/Restore, all collected as pending edits and saved together (one
// request, one revision check per item, one batch in the uncommitted changes).

state.pending = { moves: new Map(), resizes: new Map(), removes: new Set(), restores: new Set(), added: [] }

function pendingCount() {
  const p = state.pending
  return p.moves.size + p.resizes.size + p.removes.size + p.restores.size + p.added.length
}

function resetPending() {
  state.pending = { moves: new Map(), resizes: new Map(), removes: new Set(), restores: new Set(), added: [] }
  renderPending()
}

// Resolves true when there is nothing to lose or the curator chose Discard; false keeps the edits.
// Every caller awaits it (tests/test_ui_dialogs.py) and leaves its state untouched on false.
// Callers that arrive while the sheet is open (browser back/forward, a palette jump) share its answer instead of stacking another.
let discardInflight = null
async function confirmDiscardPending() {
  if (discardInflight) return discardInflight
  const count = pendingCount()
  if (!count) return true
  discardInflight = window.OF.ui.confirm({
    title: `Discard ${count} unsaved edit${count === 1 ? '' : 's'}?`,
    message: 'Moves, resizes and new points on this image will be lost.',
    confirmLabel: 'Discard', cancelLabel: 'Keep editing', danger: true,
  }).then(ok => {
    discardInflight = null
    if (ok) resetPending()
    return ok
  })
  return discardInflight
}

function imageEditable() {
  // Not while a save is in flight (its reply clears the tray), nor when the image failed to load.
  return Boolean(canDo('project-curator') && state.currentImage && !state.currentImage.source_missing
    && !state.savingPending && byId('source-image').naturalWidth > 0)
}

function markerEditable(item) {
  return Boolean(item && item.row_px != null && imageEditable())
}

// The geometry to draw for an item, including unsaved edits.
function effectiveGeometry(item) {
  const moved = state.pending.moves.get(item.id)
  return {
    row_px: moved ? moved.row_px : item.row_px,
    column_px: moved ? moved.column_px : item.column_px,
    crop_size: state.pending.resizes.get(item.id) ?? item.crop_size,
    removed: state.pending.removes.has(item.id) || (Boolean(item.removed) && !state.pending.restores.has(item.id)),
  }
}

function clampToImage(row, column) {
  const w = state.currentImage?.width || byId('source-image').naturalWidth
  const h = state.currentImage?.height || byId('source-image').naturalHeight
  return [Math.max(0, Math.min(h - 1, Math.round(row))), Math.max(0, Math.min(w - 1, Math.round(column)))]
}

const clampPatchSize = size => Math.max(32, Math.min(1024, Math.round(size)))

function queueMove(item, row, column) {
  const [r, c] = clampToImage(row, column)
  if (r === item.row_px && c === item.column_px) state.pending.moves.delete(item.id)
  else state.pending.moves.set(item.id, { row_px: r, column_px: c })
  renderPending()
}

function queueResize(item, size) {
  if (!Number.isFinite(size)) return
  const value = clampPatchSize(size)
  if (value === item.crop_size) state.pending.resizes.delete(item.id)
  else state.pending.resizes.set(item.id, value)
  renderPending()
}

function queueRemove(item, removed) {
  state.pending.removes.delete(item.id)
  state.pending.restores.delete(item.id)
  if (removed && !item.removed) state.pending.removes.add(item.id)
  if (!removed && item.removed) state.pending.restores.add(item.id)
  renderPending()
}

// A patch cut in the browser from the already-loaded image, for the tray.
function previewDataUrl(row, column, size) {
  const image = byId('source-image')
  if (!image.naturalWidth) return ''
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = 96
  canvas.getContext('2d').drawImage(image, column - size / 2, row - size / 2, size, size, 0, 0, 96, 96)
  try { return canvas.toDataURL('image/png') } catch (_) { return '' }
}

// "1 moved · 2 added · 1 removed" for the pill.
function pendingBreakdown() {
  const p = state.pending
  return [[p.moves.size, 'moved'], [p.resizes.size, 'resized'], [p.added.length, 'added'], [p.removes.size, 'removed'], [p.restores.size, 'restored']]
    .filter(([n]) => n > 0).map(([n, word]) => `${n} ${word}`).join(' · ')
}

function closePendingReview() {
  // OF.ui.closePopover closes whichever popover is open: only call it when it is this one.
  const panel = byId('pending-review-panel')
  if (!panel.hidden && window.OF?.ui?.closePopover) OF.ui.closePopover()
}

function renderPending() {
  const count = pendingCount()
  byId('pending-tray').classList.toggle('d-none', !count)
  byId('pending-count').textContent = count
  byId('pending-tray').querySelector('.pending-noun').textContent = `unsaved edit${count === 1 ? '' : 's'}`
  byId('pending-breakdown').textContent = pendingBreakdown()
  byId('pending-save').textContent = `Save ${count} edit${count === 1 ? '' : 's'}`
  if (!count) { byId('pending-message').textContent = ''; closePendingReview() }
  const rows = []
  const itemById = id => state.items.find(item => item.id === id)
  for (const [id, to] of state.pending.moves) {
    const item = itemById(id)
    if (item) rows.push({ kind: 'Move', item, text: `(${item.column_px}, ${item.row_px}) → (${to.column_px}, ${to.row_px})`, geo: effectiveGeometry(item) })
  }
  for (const [id, size] of state.pending.resizes) {
    const item = itemById(id)
    if (item) rows.push({ kind: 'Resize', item, text: `${item.crop_size} → ${size} px`, geo: effectiveGeometry(item) })
  }
  for (const id of state.pending.removes) { const item = itemById(id); if (item) rows.push({ kind: 'Remove', item, text: '', geo: effectiveGeometry(item) }) }
  for (const id of state.pending.restores) { const item = itemById(id); if (item) rows.push({ kind: 'Restore', item, text: '', geo: effectiveGeometry(item) }) }
  state.pending.added.forEach((point, index) => rows.push({ kind: 'Add', item: { current_label: point.label }, text: `(${point.column_px}, ${point.row_px}) · ${point.crop_size} px`, geo: point, addedIndex: index }))
  const preview = row => `<img alt="" src="${escapeHtml(previewDataUrl(Number(row.geo.row_px), Number(row.geo.column_px), Number(row.geo.crop_size) || 224))}">`
  byId('pending-list').innerHTML = rows.map(row => {
    if (row.addedIndex == null) {
      return `<div class="pending-row">${preview(row)}
        <span><b>${escapeHtml(row.kind)}</b> ${escapeHtml(row.item.current_label || '')} <small>${escapeHtml(row.text)}</small></span></div>`
    }
    const i = row.addedIndex
    const chips = (state.pending.added[i].hints || []).map((hint, h) =>
      `<button type="button" class="hint-chip" data-added-hint="${i}" data-hint-index="${h}" title="Use this label">${escapeHtml(hint.label)} ${Math.round(Number(hint.confidence) * 100) || 0}%</button>`).join('')
    return `<div class="pending-row pending-add">${preview(row)}
      <span class="pending-main"><b>Add</b> <small>${escapeHtml(row.text)}</small>
        <input class="form-control form-control-sm pending-label" list="relabel-options" data-added-label="${i}" value="${escapeHtml(row.item.current_label || '')}" aria-label="Label for this new point" autocomplete="off">
        <small class="text-danger d-block" data-added-error="${i}"></small>
        ${chips ? `<span class="hint-chips">${chips}</span>` : ''}</span>
      <button type="button" class="btn btn-sm btn-link" data-pending-drop="${i}">drop</button>
    </div>`
  }).join('')
  const list = byId('pending-list')
  list.querySelectorAll('[data-pending-drop]').forEach(b => b.addEventListener('click', () => {
    state.pending.added.splice(Number(b.dataset.pendingDrop), 1)
    renderMarkers()
  }))
  list.querySelectorAll('[data-added-label]').forEach(input => {
    input.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); input.dispatchEvent(new Event('change')) } })
    input.addEventListener('change', () => setAddedLabel(Number(input.dataset.addedLabel), input.value, input))
  })
  list.querySelectorAll('[data-added-hint]').forEach(chip => chip.addEventListener('click', () => {
    const index = Number(chip.dataset.addedHint)
    const hint = state.pending.added[index]?.hints?.[Number(chip.dataset.hintIndex)]
    if (hint) setAddedLabel(index, hint.label, list.querySelector(`[data-added-label="${index}"]`))
  }))
}

// Relabel one unsaved point. Only that row's input/error and its marker are touched,
// so the tray is not rebuilt under the cursor.
function setAddedLabel(index, text, input) {
  const point = state.pending.added[index]
  if (!point) return
  const error = byId('pending-list').querySelector(`[data-added-error="${index}"]`)
  const code = validRelabelCode(text)
  if (!code) {
    if (error) error.textContent = `"${String(text).trim()}" is not a label code here; keeping ${point.label}.`
    return
  }
  point.label = code
  if (error) error.textContent = ''
  if (input) input.value = code
  const marker = byId('source-markers').querySelector(`[data-pending-index="${index}"]`)
  if (marker) {
    marker.title = `New ${code} (${point.column_px}, ${point.row_px}) — not saved`
    marker.style.borderColor = labelColor(code)
    const span = marker.querySelector('span')
    if (span) span.textContent = code
  }
}

async function savePending() {
  if (!pendingCount() || !state.currentImage) return
  const p = state.pending
  // Every edit to one item carries the revision it was loaded at; the server
  // checks each item once, so a move and a resize of one point go together.
  const revisionOf = id => state.items.find(item => item.id === id)?.revision
  const edits = [
    ...[...p.moves].map(([id, to]) => ({ op: 'move', item_id: id, expected_revision: revisionOf(id), ...to })),
    ...[...p.resizes].map(([id, size]) => ({ op: 'resize', item_id: id, expected_revision: revisionOf(id), crop_size: size })),
    ...[...p.removes].map(id => ({ op: 'remove', item_id: id, expected_revision: revisionOf(id) })),
    ...[...p.restores].map(id => ({ op: 'restore', item_id: id, expected_revision: revisionOf(id) })),
  ]
  const message = byId('pending-message')
  byId('pending-save').disabled = true
  state.savingPending = true  // no new edits until the reply, which clears the tray
  try {
    const result = await request(`/api/curation/projects/${state.projectId}/source-images/${state.currentImage.first_item_id}/edits`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: byId('pending-reason').value.trim() || null, edits, added: p.added.map(({ row_px, column_px, crop_size, label }) => ({ row_px, column_px, crop_size, label })) }),
    })
    // Clear first: reloading the image below must not ask about discarding these.
    state.savingPending = false
    resetPending()
    byId('pending-reason').value = ''
    const saved = result.applied + result.added.length
    if (result.conflicts?.length) reportConflicts({ updated: saved, conflicts: result.conflicts })
    else showNotice(`Saved ${saved} edit${saved === 1 ? '' : 's'} on this image.`)
    const keep = state.selected?.id
    await selectImage(state.currentImage.first_item_id, keep)
    if (typeof refreshCommitPill === 'function') refreshCommitPill()
  } catch (error) {
    message.textContent = error.message
    message.className = 'small mt-1 mb-0 text-danger'
  } finally {
    state.savingPending = false
    byId('pending-save').disabled = false
  }
}

function syncPointTools() {
  const item = state.selected
  const onImage = Boolean(item && item.row_px != null && state.currentImage && !state.currentImage.source_missing)
  const geo = onImage ? effectiveGeometry(item) : null
  byId('point-edit-tools').classList.toggle('d-none', !onImage)
  byId('bulk-add-button').classList.toggle('d-none', !onImage)  // on the stage toolbar; it used to hide with the point tools
  if (!onImage) byId('bulk-add-form').classList.add('d-none')
  byId('point-edit-size').value = geo ? geo.crop_size : ''
  byId('point-remove').classList.toggle('d-none', !geo || geo.removed)
  byId('point-restore').classList.toggle('d-none', !geo || !geo.removed)
}

// ── Dragging the selected marker (move) or its corner handle (resize) ──────
// Positions update live on the marker only; the edit is queued once, on release.
let markerDrag = null

// True while a marker or a bulk-add candidate is being dragged (curation.js holds re-renders until it ends).
// A marker that was replaced mid-drag no longer counts: its pointer capture is gone.
function markerDragActive() {
  return Boolean(markerDrag && markerDrag.marker.isConnected)
}

function endMarkerDrag(commit) {
  const drag = markerDrag
  if (!drag) return
  markerDrag = null
  try { if (drag.marker.hasPointerCapture(drag.pointerId)) drag.marker.releasePointerCapture(drag.pointerId) } catch (_) {}
  if (commit && drag.moved) {
    drag.commit(drag)
    renderMarkers()
    syncPointTools()
  } else {
    Object.assign(drag.marker.style, drag.restore)
  }
}

// One pointer-capture drag for any marker. `begin(event)` returns the drag's own
// fields ({kind, centre, row, column, size, commit}) or null to ignore the press.
function bindMarkerDrag(marker, begin) {
  marker.addEventListener('pointerdown', event => {
    if (markerDrag && !markerDrag.marker.isConnected) markerDrag = null  // redrawn mid-drag
    if (event.button !== 0 || markerDrag) return
    const seed = begin(event)
    if (!seed) return
    event.preventDefault()
    event.stopPropagation()
    // preventDefault keeps focus where it was (perhaps a toolbar button, whose keys stay in the toolbar):
    // put it on the stage so the arrow keys nudge this point next.
    byId('source-image-wrap').focus({ preventScroll: true })
    markerDrag = {
      marker, pointerId: event.pointerId, ...seed,
      startX: event.clientX, startY: event.clientY, moved: false,
      restore: { left: marker.style.left, top: marker.style.top, width: marker.style.width, height: marker.style.height },
    }
    marker.setPointerCapture(event.pointerId)
  })
  marker.addEventListener('pointermove', event => {
    const drag = markerDrag
    if (!drag || drag.marker !== marker || event.pointerId !== drag.pointerId) return
    event.stopPropagation()
    if (!drag.moved && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 3) return
    drag.moved = true
    const point = eventToImagePoint(event)
    if (!point) return  // outside the image: keep the last position inside it
    const scale = state.markerScale || 1
    if (drag.kind === 'move') {
      ;[drag.row, drag.column] = clampToImage(point.row, point.column)
      marker.style.left = `${drag.column * scale}px`
      marker.style.top = `${drag.row * scale}px`
    } else {
      drag.size = clampPatchSize(2 * Math.max(Math.abs(point.row - drag.centre.row_px), Math.abs(point.column - drag.centre.column_px)))
      const shown = `${Math.max(12, drag.size * scale)}px`
      marker.style.width = shown
      marker.style.height = shown
    }
  })
  marker.addEventListener('pointerup', event => {
    if (!markerDrag || markerDrag.marker !== marker) return
    event.stopPropagation()
    endMarkerDrag(true)
  })
  marker.addEventListener('pointercancel', () => { if (markerDrag?.marker === marker) endMarkerDrag(false) })
  marker.addEventListener('lostpointercapture', () => { if (markerDrag?.marker === marker) endMarkerDrag(false) })
}

function wireMarkerEditing(target) {
  const selected = state.selected
  const marker = selected && markerEditable(selected) && target.querySelector(`.source-marker[data-point-id="${selected.id}"]`)
  if (marker) {
    bindMarkerDrag(marker, event => {
      const geo = effectiveGeometry(selected)
      const kind = event.target.closest('.resize-handle') ? 'resize' : 'move'
      return {
        kind, centre: geo, row: geo.row_px, column: geo.column_px, size: geo.crop_size,
        commit: drag => { if (kind === 'move') queueMove(selected, drag.row, drag.column); else queueResize(selected, drag.size) },
      }
    })
  }
  // Unsaved new points slide the same way; release writes their coordinates.
  if (!imageEditable()) return
  target.querySelectorAll('.source-marker.pending[data-pending-index]').forEach(node => {
    const entry = state.pending.added[Number(node.dataset.pendingIndex)]
    if (!entry) return
    bindMarkerDrag(node, () => ({
      kind: 'move', centre: entry, row: entry.row_px, column: entry.column_px, size: entry.crop_size,
      commit: drag => {
        if (!state.pending.added.includes(entry)) return
        ;[entry.row_px, entry.column_px] = clampToImage(drag.row, drag.column)
      },
    }))
  })
}

document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && markerDrag) {
    event.preventDefault()
    endMarkerDrag(false)
  }
})

byId('pending-save').addEventListener('click', savePending)
// Review: the edit list and the reason, as a popover above the pill.
byId('pending-review').addEventListener('click', () => OF.ui.popover(byId('pending-review'), byId('pending-review-panel'), { placement: 'above' }))
byId('pending-discard').addEventListener('click', () => { resetPending(); renderMarkers(); syncPointTools() })
byId('point-remove').addEventListener('click', () => { if (markerEditable(state.selected)) { queueRemove(state.selected, true); renderMarkers(); syncPointTools() } })
byId('point-restore').addEventListener('click', () => { if (markerEditable(state.selected)) { queueRemove(state.selected, false); renderMarkers(); syncPointTools() } })
byId('point-edit-size').addEventListener('keydown', event => {
  // Enter applies the size here instead of submitting the item form around it.
  if (event.key === 'Enter') { event.preventDefault(); event.target.dispatchEvent(new Event('change')) }
})
byId('point-edit-size').addEventListener('change', event => {
  if (!markerEditable(state.selected)) return
  if (!(Number(event.target.value) > 0)) { syncPointTools(); return }  // cleared or nonsense: show the size again
  queueResize(state.selected, Number(event.target.value))
  syncPointTools()
  renderMarkers()
})
document.querySelectorAll('[data-point-preset]').forEach(b => b.addEventListener('click', () => {
  if (!markerEditable(state.selected)) return
  queueResize(state.selected, Number(b.dataset.pointPreset))
  syncPointTools()
  renderMarkers()
}))

window.addEventListener('beforeunload', event => { if (pendingCount()) { event.preventDefault(); event.returnValue = '' } })

// ── Bulk add around a patch: candidates you slide, relabel or drop, then save ──
function fillBulkModels() {
  const select = byId('bulk-model')
  const chosen = select.value
  select.innerHTML = '<option value="">None</option>' + (state.models || []).map(model => `<option value="${escapeHtml(model.name)}">${escapeHtml(model.name)}</option>`).join('')
  if ([...select.options].some(option => option.value === chosen)) select.value = chosen
}

byId('bulk-add-button').addEventListener('click', () => {
  const form = byId('bulk-add-form')
  // The button is on the stage toolbar and the form on the Point tab: show the tab it opens in.
  if (form.classList.contains('d-none') && typeof setInspectorTab === 'function') setInspectorTab('point')
  form.classList.toggle('d-none')
  if (state.selected && state.selected.row_px != null) byId('bulk-spacing').value = Math.max(32, Math.min(512, Number(effectiveGeometry(state.selected).crop_size) || 224))
  fillBulkModels()
})

byId('bulk-run').addEventListener('click', async () => {
  const item = state.selected
  if (!item || !markerEditable(item)) return
  const image = state.currentImage
  const projectId = state.projectId
  const spacing = Number(byId('bulk-spacing').value)
  if (!(spacing >= 32 && spacing <= 512)) { showNotice('Spacing must be between 32 and 512 px.'); return }
  const model = byId('bulk-model').value
  if (state.pending.added.length >= MAX_PENDING_ADDS) { showNotice(ADD_LIMIT_MESSAGE); return }
  const geo = effectiveGeometry(item)  // centre on the point as shown, an unsaved move included
  const run = byId('bulk-run')
  run.disabled = true
  try {
    const result = await request(`/api/curation/projects/${projectId}/source-images/${image.first_item_id}/suggest`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item_id: item.id, grid: Number(byId('bulk-grid').value), spacing_px: spacing,
        row_px: geo.row_px, column_px: geo.column_px, crop_size: geo.crop_size, model_name: model || null }),
    })
    // The reply is for the image it was asked on; drop it if the page moved on or began saving.
    if (state.projectId !== projectId || state.currentImage !== image || !imageEditable()) return
    if (!addCandidates(result.candidates, item.current_label)) return  // over the limit: told, form stays open
    byId('bulk-add-form').classList.add('d-none')
    if (model && !result.model_available) showNotice('No classifier available here; candidates use the source label only.')
  } catch (error) { showNotice(error.message) } finally { run.disabled = false }
})

// The server takes at most 200 new points in one save and refuses a larger batch whole.
const MAX_PENDING_ADDS = 200
const ADD_LIMIT_MESSAGE = `Up to ${MAX_PENDING_ADDS} new points can be saved at once. Save these edits, then bulk add again.`

function addCandidates(candidates, label) {
  candidates = candidates || []
  if (state.pending.added.length + candidates.length > MAX_PENDING_ADDS) {
    showNotice(`${ADD_LIMIT_MESSAGE} (${state.pending.added.length} pending, ${candidates.length} more suggested.)`)
    return false
  }
  for (const candidate of candidates) {
    state.pending.added.push({ row_px: candidate.row_px, column_px: candidate.column_px, crop_size: candidate.crop_size,
      label, hints: candidate.hints || [] })
  }
  renderPending()
  renderMarkers()
  return true
}

// ── Keyboard: nudge / remove the selected point, step through the image queue ──
let steppingImage = false

async function stepImage(direction) {
  if (steppingImage || !state.currentImage) return
  const at = state.images.findIndex(image => image.first_item_id === state.currentImage.first_item_id)
  const next = state.images[at + direction]
  if (at < 0 || !next) return  // end of the loaded list: nothing to do
  steppingImage = true
  try { await selectImage(next.first_item_id) } catch (error) { showNotice(error.message) } finally { steppingImage = false }
}

document.addEventListener('keydown', event => {
  if (state.view !== 'source' || event.defaultPrevented || event.ctrlKey || event.metaKey) return
  if (event.target.closest?.('input, textarea, select, [contenteditable]:not([contenteditable="false"])') || document.querySelector('dialog[open]')) return
  const item = state.selected
  const editable = markerEditable(item)
  const arrow = event.key === 'ArrowLeft' || event.key === 'ArrowRight'
  if (arrow && (event.altKey || !editable)) {
    event.preventDefault()
    stepImage(event.key === 'ArrowRight' ? 1 : -1)
    return
  }
  if (!editable) return
  const step = event.shiftKey ? 10 : 1
  const moves = { ArrowUp: [-step, 0], ArrowDown: [step, 0], ArrowLeft: [0, -step], ArrowRight: [0, step] }
  const geo = effectiveGeometry(item)
  if (moves[event.key] && !event.altKey) {
    event.preventDefault()
    if (geo.removed || markerDrag) return
    queueMove(item, geo.row_px + moves[event.key][0], geo.column_px + moves[event.key][1])
    renderMarkers()
  } else if (event.key === 'Delete' || event.key === 'Backspace') {
    event.preventDefault()
    if (event.repeat || markerDrag) return
    queueRemove(item, !geo.removed)
    renderMarkers()
    syncPointTools()
  }
})

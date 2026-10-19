'use strict'

const state = { projects: [], projectId: null, view: 'source', images: [], currentImage: null, items: [], selected: null, sourceCursor: null, sourceCursorMissing: 0, sourceQueueLoaded: false, crops: [], cropCursor: null, selectedCropIds: new Set(), cropGridSize: 'medium', resolvedCropUris: new Map(), qualityItems: [], qualityRunId: 0, qualityScope: 'patch', qualityCursor: null, qualityTotal: null, taxonomy: [], facets: null, selectedTaxonomy: new Map(), selectedCodes: new Set(), matrix: null, matrixFilter: null, matrixLinkedItemIds: [], matrixZoom: 1, evaluationItems: [], evaluationCursor: null, evaluationSplit: 'test', evaluationSort: 'confidence_desc', matrixClassModes: { train: 'both', val: 'both' }, selectedMatrixClasses: new Set(), selectedMatrixItemIds: new Set(), matrixGridSize: 'medium', lassoGrid: null, suppressGridClickUntil: 0, models: [], addPointMode: false, imageView: { zoom: 1, panX: 0, panY: 0, dragging: false, moved: false, startX: 0, startY: 0 }, importId: null, importTimer: null, backTo: null, pendingImage: null }
const byId = id => document.getElementById(id)
const evaluationState = { runs: [], models: [], benchmarks: [], timer: null, editContext: null, preferred: null, projectId: null, assetRequest: 0 }
const proxyUrl = (uri, rawOrientation = false) => `/api/curation/image?uri=${encodeURIComponent(uri)}${rawOrientation ? '&raw_orientation=true' : ''}`
// A moved, resized or added point has no stored crop that matches it: always the fresh crop-preview.
const cropPreviewUrl = item => state.resolvedCropUris.has(item.id) && !item.metadata?.geometry_edited && !item.metadata?.manual_point
  ? proxyUrl(state.resolvedCropUris.get(item.id))
  : `/api/curation/projects/${state.projectId}/items/${item.id}/crop-preview?r=${item.revision ?? 0}`

// Who is signed in, and what they may change. Filled in by init().
const session = { user: null, authEnabled: false }
const ROLE_ORDER = ['viewer', 'curator', 'lead', 'admin']
// 'create' is the bar for adding projects and evaluation models (CURATION_CREATE_ROLE
// on the server, curator by default). Imports and evaluations use 'project-curator'.
const CREATE_ROLE = document.body.dataset.createRole || 'lead'
const roleFor = role => role === 'create' ? CREATE_ROLE : role
// Project work follows the project role (members only); app-wide admins lead everywhere.
const projectRole = () => currentProject()?.my_role || null
const canDo = role => {
  if (!session.user) return false
  if (role === 'project-curator') return session.user.role === 'admin' || ['curator', 'lead'].includes(projectRole())
  if (role === 'project-lead') return session.user.role === 'admin' || projectRole() === 'lead'
  return ROLE_ORDER.indexOf(session.user.role) >= ROLE_ORDER.indexOf(roleFor(role))
}
const ROLE_NAMES = { 'project-curator': 'project curator', 'project-lead': 'project lead' }

class RequestError extends Error {
  constructor(message, status, detail) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

async function request(url, options = {}) {
  const response = await fetch(url, { credentials: 'same-origin', ...options })
  if (response.status === 401) {
    // Session ended mid-task: come back here after signing in again.
    window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search + window.location.hash)}`)
    return new Promise(() => {})
  }
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const message = typeof body.detail === 'string' ? body.detail : body.detail?.message || 'Request failed'
    throw new RequestError(message, response.status, body.detail)
  }
  return body
}

function showNotice(message) {
  byId('curation-notice-text').textContent = message
  byId('curation-notice').classList.remove('d-none')
}

function applyRoleGates() {
  // Controls tagged data-min-role are disabled — not hidden, so a viewer can
  // still see what curators can do — with a tooltip saying why.
  document.querySelectorAll('[data-min-role]').forEach(control => {
    const allowed = canDo(control.dataset.minRole)
    control.disabled = !allowed
    control.classList.toggle('role-locked', !allowed)
    if (!allowed) control.title = `Needs the ${ROLE_NAMES[control.dataset.minRole] || roleFor(control.dataset.minRole)} role`
  })
  byId('empty-projects-role').hidden = canDo('create')
}

function timeAgo(iso) {
  if (!iso) return ''
  const seconds = Math.max(0, (Date.now() - new Date(iso.endsWith('Z') ? iso : `${iso}Z`).getTime()) / 1000)
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`
  return `${Math.round(seconds / 86400)} d ago`
}

async function loadProjects() {
  state.projects = await request('/api/curation/projects')
  const select = byId('project-select')
  select.innerHTML = state.projects.map(project => `<option value="${project.id}">${escapeHtml(project.name)}</option>`).join('')
  byId('empty-projects').classList.toggle('d-none', state.projects.length > 0)
  byId('curation-workspace').classList.toggle('d-none', state.projects.length === 0)
  byId('project-select').classList.toggle('d-none', state.projects.length === 0)
  renderFilterChipBar()
  if (state.projects.length) {
    // Keep the project the curator was in (or just created) across reloads.
    state.projectId = state.projects.some(project => project.id === state.projectId) ? state.projectId : state.projects[0].id
    try { localStorage.setItem('curation-project', state.projectId) } catch (_) {}
    select.value = state.projectId
    resetSourceWorkspace()
    renderProjectMode()
    renderTaxonomyOptions()
    if (typeof resetChangeFeed === 'function') await resetChangeFeed()  // live.js: before any list loads
    await loadEvaluationCatalog()
    setView(state.pendingView || 'matrix')
    state.pendingView = null
    Promise.all([loadFacets(), loadStats(), loadLatestImport()]).catch(error => showNotice(error.message))
  }
}

async function loadTaxonomy() {
  state.taxonomy = await request('/api/curation/taxonomy')
  renderTaxonomyOptions()
}

function renderTaxonomyOptions() {
  const options = byId('taxonomy-options')
  if (!options) return
  const mode = currentProject()?.dataset_mode || 'T3'
  const tiers = new Set(['TIER_1', 'TIER_2', 'TIER_2b', 'TIER_3'])
  options.innerHTML = state.taxonomy
    .filter(node => tiers.has(node.tier) && node.codes.length && (mode !== 'T1' || node.tier === 'TIER_1'))
    .map(node => `<option value="${escapeHtml(taxonomyDisplay(node))}">${node.codes.length} codes</option>`)
    .join('')
  const relabel = byId('relabel-options')
  // Mirror curation/taxonomy.py project_code: a target-tier class code stands for
  // itself; a leaf code is an alias only when it falls under exactly one class.
  const targetTier = mode === 'T1' ? 'TIER_1' : 'TIER_3'
  const targets = state.taxonomy.filter(node => node.tier === targetTier && node.code)
  const projection = new Map()
  const direct = new Map()
  targets.forEach(node => direct.set(node.code, (direct.get(node.code) || 0) + 1))
  direct.forEach((count, code) => { if (count === 1) projection.set(code, code) })
  const owners = new Map()
  targets.forEach(node => node.codes.forEach(code => {
    if (!owners.has(code)) owners.set(code, new Set())
    owners.get(code).add(node.code)
  }))
  owners.forEach((classes, code) => { if (classes.size === 1 && !projection.has(code)) projection.set(code, [...classes][0]) })
  state.relabelCodes = projection
  const classCodes = [...direct.keys()].filter(code => projection.has(code)).sort()
  const aliases = [...projection.keys()].filter(code => !direct.has(code)).sort()
  relabel.innerHTML = [...classCodes, ...aliases].map(code => `<option value="${escapeHtml(code)}"></option>`).join('')
}

// A relabel target must be a code the project's taxonomy projects to one class;
// the server refuses anything else, so say so before sending rather than after.
// Returns the code the server will store.
function validRelabelCode(text) {
  const code = (text || '').trim().replace(/^\*+/, '').trim()
  if (!code) return null
  if (!state.relabelCodes || !state.relabelCodes.size) return code  // taxonomy not loaded yet: let the server decide
  return state.relabelCodes.get(code) || null
}

function taxonomyDisplay(node) { return `${node.tier.replaceAll('_', ' ')}: ${node.name}` }

async function loadFacets() {
  if (!state.projectId) return
  const params = currentFilters()
  params.delete('limit')
  state.facets = await request(`/api/curation/projects/${state.projectId}/facets?${params}`)
  populateFacet('filter-region', 'All regions', state.facets.regions)
  populateFacet('filter-island', 'All islands', state.facets.islands)
  populateFacet('filter-year', 'All years', state.facets.years)
  populateFacet('filter-site', 'All sites', state.facets.sites)
  populateFacet('filter-annotator', 'All annotators', state.facets.annotators)
  byId('code-options').innerHTML = state.facets.labels.map(item => `<option value="${escapeHtml(item.value)}">${item.count.toLocaleString()} items</option>`).join('')
  renderFilterChipBar()  // a facet the new selection no longer offers was just reset
}

function populateFacet(id, emptyLabel, values) {
  const select = byId(id)
  const selected = select.value
  select.innerHTML = `<option value="">${emptyLabel}</option>` + values.map(item => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.value)} (${item.count.toLocaleString()})</option>`).join('')
  if ([...select.options].some(option => option.value === selected)) select.value = selected
}

function currentProject() { return state.projects.find(project => project.id === state.projectId) }

function renderProjectMode() {
  const project = currentProject()
  byId('dataset-mode-badge').textContent = project?.dataset_mode || 'T3'
  byId('project-owner').textContent = project
    ? `Owner: ${project.curator_name || 'unknown'} · ${project.item_count.toLocaleString()} items${project.source_config?.annotation_pool === 'original_unsplit_oasis' ? ' · Full unsplit annotation pool' : ''}`
    : ''
  applyRoleGates()
  updateBulkControls()
  updateMatrixBulkControls()
  if (typeof renderMembership === 'function') renderMembership()
}

async function loadLatestImport() {
  if (!state.projectId) return
  const operations = await request(`/api/curation/projects/${state.projectId}/imports`)
  // The server leaves availability checks out; skip any that slip through all the same.
  renderImport(operations.find(operation => !isAvailabilityCheck(operation)) || null)
}

// An availability check runs as an import operation, but Dataset status owns it.
function isAvailabilityCheck(operation) {
  return Boolean(operation?.sources?.length) &&
    operation.sources.every(source => ['availability_check', 'quality_scores'].includes(source.kind))
}

function renderImport(operation) {
  clearTimeout(state.importTimer)
  if (isAvailabilityCheck(operation)) operation = null
  const panel = byId('import-status')
  const completed = operation?.status === 'completed'
  panel.classList.toggle('d-none', !operation || completed)
  byId('import-noaa-button').disabled = !canDo('project-curator')
  // A Hugging Face import that stopped short can be read again; rows that landed are skipped (hub.js).
  const resumable = currentProject()?.source_kind === 'huggingface' && canDo('project-curator') &&
    ['failed', 'canceled', 'interrupted'].includes(operation?.status)
  byId('resume-import').classList.toggle('d-none', !resumable)
  byId('resume-import').disabled = !resumable
  if (!operation || completed) {
    state.importId = null
    return
  }
  state.importId = operation.id
  byId('import-state').textContent = `Import ${operation.status}${operation.created_by_name ? ` · started by ${operation.created_by_name}` : ''}`
  byId('import-source').textContent = operation.error_message || operation.current_source || ''
  byId('import-processed').textContent = operation.processed_rows.toLocaleString()
  byId('import-added').textContent = operation.imported_rows.toLocaleString()
  byId('import-skipped').textContent = operation.skipped_rows.toLocaleString()
  byId('import-rejected').textContent = operation.rejected_rows.toLocaleString()
  const active = ['queued', 'starting', 'running'].includes(operation.status)
  byId('cancel-import').classList.toggle('d-none', !active)
  byId('import-noaa-button').disabled = active || !canDo('project-curator')
  if (active) {
    state.importTimer = setTimeout(async () => {
      const current = await request(`/api/curation/projects/${state.projectId}/imports/${operation.id}`)
      renderImport(current)
      if (!['queued', 'starting', 'running'].includes(current.status)) {
        // Reloading projects resets the source workspace: not while edits are unsaved,
        // and never for an availability check (Dataset status refreshes for those).
        if (isAvailabilityCheck(current) || (typeof pendingCount === 'function' && pendingCount())) await loadStats()
        else await Promise.all([loadQueue(false), loadStats(), loadProjects()])
      }
    }, 2000)
  }
}

async function startNoaaImport() {
  if (!state.projectId) return
  const defaults = await request('/api/curation/import-sources')
  const operation = await request(`/api/curation/projects/${state.projectId}/imports`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sources: defaults.sources, batch_size: 2000 }),
  })
  renderImport(operation)
}

async function cancelImport() {
  if (!state.importId) return
  const operation = await request(`/api/curation/projects/${state.projectId}/imports/${state.importId}/cancel`, { method: 'POST' })
  renderImport(operation)
}

function currentFilters() {
  const params = new URLSearchParams({ limit: '30' })
  const imageSearch = byId('filter-image').value.trim()
  const reviewState = byId('filter-state').value
  const qualityDecision = byId('quality-filter') ? byId('quality-filter').value : ''
  const included = byId('filter-included').value
  const codes = new Set(state.selectedCodes)
  state.selectedTaxonomy.forEach(node => node.codes.forEach(code => codes.add(code)))
  if (imageSearch) params.set('image_search', imageSearch)
  if (codes.size) params.set('labels', [...codes].join(','))
  // A saved run's predictions cell is filtered on the server, so every view gets all of its items, not
  // just the evidence cards loaded so far. Dataset members and change sets still send the loaded ids.
  const runCell = state.matrixFilter?.runId && byId('evaluation-evidence-kind').value !== 'inventory'
  if (runCell) {
    params.set('eval_run', state.matrixFilter.runId)
    if (state.matrixFilter.actual) params.set('eval_actual', state.matrixFilter.actual)
    if (state.matrixFilter.predicted) params.set('eval_predicted', state.matrixFilter.predicted)
    params.set('eval_split', state.evaluationSplit)
  } else {
    if (state.matrixFilter?.actual) {
      if (state.matrixFilter.actualAxis === 'source') params.set('source_labels', state.matrixFilter.actual)
      else params.set('labels', [...codes, state.matrixFilter.actual].join(','))
    }
    if (state.matrixFilter?.modelCodes?.length) params.set('model_labels', state.matrixFilter.modelCodes.join(','))
    if (state.matrixFilter?.evaluation) params.set('item_ids', state.matrixLinkedItemIds.length ? state.matrixLinkedItemIds.join(',') : '0')
  }
  if (byId('filter-split').value) params.set('item_split', byId('filter-split').value)
  if (reviewState) params.set('review_state', reviewState)
  if (qualityDecision) params.set('quality_decision', qualityDecision)
  if (included) params.set('included', included)
  if (byId('filter-region').value) params.set('region', byId('filter-region').value)
  if (byId('filter-island').value) params.set('island', byId('filter-island').value)
  if (byId('filter-year').value) params.set('year', byId('filter-year').value)
  if (byId('filter-site').value) params.set('site', byId('filter-site').value)
  if (byId('filter-annotator').value) params.set('annotator', byId('filter-annotator').value)
  if (byId('filter-origin').value) params.set('label_origin', byId('filter-origin').value)
  params.set('source_missing', byId('filter-hide-missing').checked ? 'hide' : 'any')
  if (byId('filter-show-removed').checked) params.set('removed', 'any')
  if (byId('filter-hide-claimed').checked) params.set('hide_claimed', 'true')  // read by the item and image lists only
  return params
}

function addTaxonomyFilter() {
  const value = byId('taxonomy-filter').value.trim()
  const node = state.taxonomy.find(candidate => taxonomyDisplay(candidate).toLowerCase() === value.toLowerCase())
  if (!node) return
  state.selectedTaxonomy.set(node.key, node)
  byId('taxonomy-filter').value = ''
  renderFilterChips()
}

function addCodeFilters() {
  byId('filter-label').value.split(/[\s,]+/).map(value => value.trim().replace(/^\*/, '').toUpperCase()).filter(Boolean).forEach(code => state.selectedCodes.add(code))
  byId('filter-label').value = ''
  renderFilterChips()
}

function renderFilterChips() {
  byId('taxonomy-chips').innerHTML = [...state.selectedTaxonomy].map(([key, node]) => `<button type="button" data-remove-taxon="${key}">${escapeHtml(node.name)} <i class="bi bi-x"></i></button>`).join('')
  byId('code-chips').innerHTML = [...state.selectedCodes].map(code => `<button type="button" data-remove-code="${escapeHtml(code)}">${escapeHtml(code)} <i class="bi bi-x"></i></button>`).join('')
  document.querySelectorAll('[data-remove-taxon]').forEach(button => button.addEventListener('click', () => { state.selectedTaxonomy.delete(button.dataset.removeTaxon); renderFilterChips(); cascadeFilters() }))
  document.querySelectorAll('[data-remove-code]').forEach(button => button.addEventListener('click', () => { state.selectedCodes.delete(button.dataset.removeCode); renderFilterChips(); cascadeFilters() }))
  renderFilterChipBar()
}

// ── Filter chip bar: one removable chip per active filter, under the top bar ──
// The controls themselves live in #filters-popover; the chips only read them.
const FILTER_CHIP_SELECTS = [
  // [control id, chip label, show the option text (static options) rather than the value (facets)]
  ['filter-state', 'State', true],
  ['filter-included', 'Inclusion', true],
  ['filter-split', 'Split', true],
  ['filter-origin', 'Origin', true],
  ['filter-region', 'Region', false],
  ['filter-island', 'Island', false],
  ['filter-year', 'Year', false],
  ['filter-site', 'Site', false],
  ['filter-annotator', 'Annotator', false],
]

function renderFilterChipBar() {
  const bar = byId('filter-chips')
  if (!bar) return
  // A focused toggle chip keeps focus across the re-render.
  const focusedToggle = bar.contains(document.activeElement) ? document.activeElement.dataset.chipToggle : null
  const chip = (label, attrs) => `<button type="button" class="chip chip-active" ${attrs} aria-label="Remove filter ${escapeHtml(label)}">${escapeHtml(label)} <span class="chip-x" aria-hidden="true">✕</span></button>`
  const chips = []
  const image = byId('filter-image').value.trim()
  if (image) chips.push(chip(`Image: ${image}`, 'data-chip-clear="filter-image"'))
  state.selectedTaxonomy.forEach((node, key) => chips.push(chip(`Taxonomy: ${node.name}`, `data-chip-taxon="${escapeHtml(key)}"`)))
  state.selectedCodes.forEach(code => chips.push(chip(`Code: ${code}`, `data-chip-code="${escapeHtml(code)}"`)))
  FILTER_CHIP_SELECTS.forEach(([id, label, useText]) => {
    const select = byId(id)
    if (!select || !select.value) return
    const shown = useText ? select.options[select.selectedIndex]?.text || select.value : select.value
    chips.push(chip(`${label}: ${shown}`, `data-chip-clear="${id}"`))
  })
  const active = chips.length
  const toggle = (id, label) => {
    const on = byId(id).checked
    return `<button type="button" class="chip${on ? ' chip-active' : ''}" data-chip-toggle="${id}" aria-pressed="${on}">${on ? '<i class="bi bi-check2" aria-hidden="true"></i> ' : ''}${escapeHtml(label)}</button>`
  }
  chips.push(toggle('filter-hide-missing', 'Hide missing images'), toggle('filter-show-removed', 'Show removed'))
  if (state.view === 'source') chips.push(toggle('filter-hide-handled', 'Hide finished images'))
  chips.push(toggle('filter-hide-claimed', "Hide others' open work"))
  // A saved evaluation is a fixed snapshot: say so rather than let active filters look ignored.
  const fixedMatrix = state.view === 'matrix' && (selectedRunId() || byId('matrix-source')?.value === 't3_v0_test')
  if (active && fixedMatrix) chips.push(`<span class="chip-note">Filters don't change a saved evaluation's matrix</span>`)
  bar.innerHTML = chips.join('')
  if (focusedToggle) bar.querySelector(`[data-chip-toggle="${focusedToggle}"]`)?.focus()
  const count = byId('filter-count')
  if (count) count.textContent = active ? `${active} filter${active === 1 ? '' : 's'}` : ''
  const button = byId('filters-button')
  if (button) button.classList.toggle('active', active > 0)
}

byId('filter-chips').addEventListener('click', event => {
  const target = event.target.closest('button')
  if (!target) return
  if (target.dataset.chipToggle) {
    const checkbox = byId(target.dataset.chipToggle)
    checkbox.checked = !checkbox.checked
    checkbox.dispatchEvent(new Event('change'))
    renderFilterChipBar()
    return
  }
  if (target.dataset.chipTaxon !== undefined) { state.selectedTaxonomy.delete(target.dataset.chipTaxon); renderFilterChips() }
  else if (target.dataset.chipCode !== undefined) { state.selectedCodes.delete(target.dataset.chipCode); renderFilterChips() }
  else if (target.dataset.chipClear) byId(target.dataset.chipClear).value = ''
  else return
  renderFilterChipBar()
  cascadeFilters()
  byId('filters-button').focus()  // the chip that had focus was just re-rendered away
})

byId('filters-button').addEventListener('click', () => OF.ui.popover(byId('filters-button'), byId('filters-popover')))

// Project actions (import, new project, registry, members, releases, delete) sit in the More menu.
byId('topbar-more').addEventListener('click', () => OF.ui.popover(byId('topbar-more'), byId('topbar-more-menu')))
byId('topbar-more-menu').addEventListener('click', event => { if (event.target.closest('button')) OF.ui.closePopover() })
// Keys typed in any popover stay there (OF.ui.popover stops them), so the source editor (editor.js) never reads
// them as point moves, image steps or a removal.

function applyActiveFilters() {
  if (state.view === 'source') return loadQueue(false)
  if (state.view === 'quality') return loadQualityReview()
  return loadCrops(false)
}

function clearFilters() {
  state.selectedTaxonomy.clear()
  state.selectedCodes.clear()
  state.matrixFilter = null
  ;['filter-image', 'filter-label'].forEach(id => { byId(id).value = '' })
  ;['taxonomy-filter', 'filter-state', 'filter-included', 'filter-split', 'quality-filter', 'filter-region', 'filter-island', 'filter-year', 'filter-site', 'filter-annotator', 'filter-origin'].forEach(id => { if (byId(id)) byId(id).value = '' })
  byId('filter-hide-missing').checked = true
  byId('filter-show-removed').checked = false
  byId('filter-hide-handled').checked = true
  byId('filter-hide-claimed').checked = true
  renderFilterChips()
  renderMatrixDrilldown()
  renderFilterChipBar()
  cascadeFilters()
}

async function cascadeFilters() {
  try {
    await loadFacets()
    await applyActiveFilters()
  } finally {
    renderFilterChipBar()
  }
}

function exportFiltered() {
  if (!state.projectId) return
  const params = currentFilters()
  params.delete('limit')
  params.set('export_format', byId('export-format').value)
  window.location.href = `/api/curation/projects/${state.projectId}/export?${params}`
}

async function deleteProject() {
  const project = currentProject()
  if (!project) return
  const typed = await window.OF.ui.prompt({
    title: `Delete ${project.name}?`, message: 'This deletes the project, its items, versions and history. Type the project name to confirm.',
    label: 'Project name', confirmLabel: 'Delete project', danger: true,
    validate: value => value === project.name ? null : 'The name does not match.',
  })
  if (typed === null) return
  // After the name, so cancelling the delete never costs the unsaved edits.
  if (typeof confirmDiscardPending === 'function' && !(await confirmDiscardPending())) return
  await request(`/api/curation/projects/${project.id}?confirm_name=${encodeURIComponent(typed)}`, { method: 'DELETE' })
  state.projectId = null
  state.selectedTaxonomy.clear()
  state.selectedCodes.clear()
  await loadProjects()
}

async function loadQueue(append) {
  if (!state.projectId) return
  const params = currentFilters()
  if (byId('filter-hide-handled').checked) params.set('handled', 'hide')
  if (append && state.sourceCursor) {
    params.set('after_id', state.sourceCursor)
    params.set('after_missing', state.sourceCursorMissing || 0)
  }
  if (!append) byId('queue-list').innerHTML = skeletonRows(6)  // replaced by renderQueue
  let page
  try { page = await request(`/api/curation/projects/${state.projectId}/source-images?${params}`) } catch (error) { renderQueue(); throw error }
  state.images = append ? [...state.images, ...page.images] : page.images
  state.sourceQueueLoaded = true
  state.sourceCursor = page.next_cursor
  state.sourceCursorMissing = page.next_missing || 0
  byId('load-more').classList.toggle('d-none', !state.sourceCursor)
  renderQueue()
}

function resetSourceWorkspace() {
  if (typeof resetPending === 'function') resetPending()  // the image those edits were on is gone
  state.images = []
  byId('bulk-add-form').classList.add('d-none')
  state.currentImage = null
  state.items = []
  state.selected = null
  state.sourceCursor = null
  state.sourceCursorMissing = 0
  state.sourceQueueLoaded = false
  byId('source-image').removeAttribute('src')
  byId('source-markers').innerHTML = ''
  byId('point-legend').innerHTML = ''
  byId('annotation-table-body').innerHTML = ''
  byId('no-selection').classList.remove('d-none')
  byId('paired-view').classList.add('d-none')
  byId('inspector-empty').classList.remove('d-none')
  byId('item-form').classList.add('d-none')
  byId('annotation-table-panel').classList.add('d-none')
  byId('load-more').classList.add('d-none')
  byId('item-metadata').innerHTML = ''
  renderImageFacts()
  renderModelContext()
  renderQueue()
}

async function loadCrops(append) {
  if (!state.projectId) return
  const params = currentFilters()
  params.set('limit', '60')
  if (append && state.cropCursor) params.set('after_id', state.cropCursor)
  if (!append) byId('crop-grid').innerHTML = skeletonTiles(12)  // replaced by renderCropGrid
  let page
  try { page = await request(`/api/curation/projects/${state.projectId}/items?${params}`) } catch (error) {
    if (!append) state.crops = []
    renderCropGrid(!append)  // a failed first page says so, rather than claiming nothing matches
    throw error
  }
  state.crops = append ? [...state.crops, ...page.items] : page.items
  if (!append) state.selectedCropIds.clear()
  state.cropCursor = page.next_cursor
  byId('load-more-crops').classList.toggle('d-none', !state.cropCursor)
  renderCropGrid()
}

function getGridMinSize(sizeKey) {
  const sizes = { small: 120, medium: 150, large: 190 }
  return sizes[sizeKey] || 150
}

function applyGridSizing() {
  const cropMin = getGridMinSize(state.cropGridSize)
  byId('crop-grid')?.style.setProperty('--crop-grid-min', `${cropMin}px`)
  const matrixMin = getGridMinSize(state.matrixGridSize)
  byId('evaluation-grid')?.style.setProperty('--crop-grid-min', `${matrixMin}px`)
}

function updateSelectionState(set, ids, operation) {
  if (operation === 'select') ids.forEach(id => set.add(id))
  else if (operation === 'deselect') ids.forEach(id => set.delete(id))
  else if (operation === 'toggle') ids.forEach(id => { if (set.has(id)) set.delete(id); else set.add(id) })
}

function moveGridFocus(grid, currentCard, key) {
  const cards = [...grid.querySelectorAll('.curation-crop-card[tabindex="0"]')]
  const currentRect = currentCard.getBoundingClientRect()
  const currentCenter = { x: currentRect.left + currentRect.width / 2, y: currentRect.top + currentRect.height / 2 }
  const vertical = key === 'ArrowUp' || key === 'ArrowDown'
  const reverse = key === 'ArrowUp' || key === 'ArrowLeft'
  const candidates = cards.filter(card => {
    if (card === currentCard) return false
    const rect = card.getBoundingClientRect()
    const center = vertical ? rect.top + rect.height / 2 : rect.left + rect.width / 2
    const origin = vertical ? currentCenter.y : currentCenter.x
    return reverse ? center < origin - 1 : center > origin + 1
  })
  candidates.sort((left, right) => {
    const score = card => {
      const rect = card.getBoundingClientRect()
      const center = { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }
      const primary = Math.abs((vertical ? center.y : center.x) - (vertical ? currentCenter.y : currentCenter.x))
      const secondary = Math.abs((vertical ? center.x : center.y) - (vertical ? currentCenter.x : currentCenter.y))
      return primary * 1000 + secondary
    }
    return score(left) - score(right)
  })
  if (candidates[0]) {
    candidates[0].focus()
    candidates[0].scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }
}

function setupGridKeyboard(gridId, selectionSet, updateControls) {
  const grid = byId(gridId)
  grid.querySelectorAll('.curation-crop-card').forEach(card => card.addEventListener('keydown', event => {
    if (event.target !== card) return
    if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(event.key)) {
      event.preventDefault()
      moveGridFocus(grid, card, event.key)
      return
    }
    if (event.key !== ' ' || !card.dataset.gridSelectId) return
    event.preventDefault()
    const itemId = Number(card.dataset.gridSelectId)
    updateSelectionState(selectionSet, [itemId], 'toggle')
    card.classList.toggle('selected', selectionSet.has(itemId))
    const checkbox = card.querySelector('.crop-select')
    if (checkbox) checkbox.checked = selectionSet.has(itemId)
    updateControls()
  }))
}

function syncLassoControls() {
  ;[['crop', 'lasso-crops', 'crop-grid'], ['matrix', 'lasso-matrix', 'evaluation-grid']].forEach(([scope, buttonId, gridId]) => {
    const active = state.lassoGrid === scope
    byId(buttonId).classList.toggle('active', active)
    byId(buttonId).setAttribute('aria-pressed', String(active))
    byId(gridId).classList.toggle('lasso-active', active)
  })
}

function toggleLasso(scope) {
  state.lassoGrid = state.lassoGrid === scope ? null : scope
  syncLassoControls()
}

function setupGridLasso(gridId, scope, selectionSet, updateControls) {
  const grid = byId(gridId)
  grid._lassoConfig = { selectionSet, updateControls }
  if (grid.dataset.lassoReady) return
  grid.dataset.lassoReady = 'true'
  grid.addEventListener('pointerdown', event => {
    if (state.lassoGrid !== scope || event.button !== 0 || event.target.closest('button, input, select, a')) return
    event.preventDefault()
    const start = { x: event.clientX, y: event.clientY }
    const overlay = document.createElement('div')
    overlay.className = 'grid-lasso-box'
    document.body.appendChild(overlay)
    let moved = false
    const updateLasso = currentEvent => {
      const left = Math.min(start.x, currentEvent.clientX)
      const top = Math.min(start.y, currentEvent.clientY)
      const right = Math.max(start.x, currentEvent.clientX)
      const bottom = Math.max(start.y, currentEvent.clientY)
      moved ||= Math.abs(currentEvent.clientX - start.x) > 4 || Math.abs(currentEvent.clientY - start.y) > 4
      Object.assign(overlay.style, { left: `${left}px`, top: `${top}px`, width: `${right - left}px`, height: `${bottom - top}px` })
      grid.querySelectorAll('[data-grid-select-id]').forEach(card => {
        const rect = card.getBoundingClientRect()
        card.classList.toggle('lasso-hit', rect.left < right && rect.right > left && rect.top < bottom && rect.bottom > top)
      })
    }
    const finishLasso = currentEvent => {
      window.removeEventListener('pointermove', updateLasso)
      window.removeEventListener('pointerup', finishLasso)
      overlay.remove()
      const cards = [...grid.querySelectorAll('[data-grid-select-id].lasso-hit')]
      cards.forEach(card => card.classList.remove('lasso-hit'))
      if (!moved) return
      const operation = currentEvent.altKey ? 'deselect' : currentEvent.ctrlKey || currentEvent.metaKey ? 'toggle' : 'select'
      const config = grid._lassoConfig
      updateSelectionState(config.selectionSet, cards.map(card => Number(card.dataset.gridSelectId)), operation)
      cards.forEach(card => {
        const selected = config.selectionSet.has(Number(card.dataset.gridSelectId))
        card.classList.toggle('selected', selected)
        const checkbox = card.querySelector('.crop-select')
        if (checkbox) checkbox.checked = selected
      })
      state.suppressGridClickUntil = Date.now() + 250
      config.updateControls()
    }
    window.addEventListener('pointermove', updateLasso)
    window.addEventListener('pointerup', finishLasso)
  })
}

// Shared empty state and skeleton markup. Everything interpolated is a literal from the callers.
function emptyStateHtml(icon, title, message, actionId, actionLabel) {
  return `<div class="empty-state"><i class="bi ${icon}" aria-hidden="true"></i><strong>${escapeHtml(title)}</strong><p>${escapeHtml(message)}</p>${actionId ? `<button id="${actionId}" class="btn btn-sm" type="button">${escapeHtml(actionLabel)}</button>` : ''}</div>`
}

function skeletonRows(count) {
  return Array.from({ length: count }, () => '<div class="skeleton skeleton-row" aria-hidden="true"></div>').join('')
}

function skeletonTiles(count) {
  return Array.from({ length: count }, () => '<div class="skeleton skeleton-tile" aria-hidden="true"></div>').join('')
}

function renderCropGrid(failed = false) {
  applyGridSizing()
  byId('crop-grid-count').textContent = `${state.crops.length} loaded`
  const grid = byId('crop-grid')
  grid.innerHTML = state.crops.length ? state.crops.map(item => `<article class="curation-crop-card tile ${state.selectedCropIds.has(item.id) ? 'selected' : ''}" data-crop-id="${item.id}" data-grid-select-id="${item.id}" style="--label-color:${labelColor(item.current_label)}" tabindex="0">
    <input class="crop-select" type="checkbox" data-select-crop="${item.id}" ${state.selectedCropIds.has(item.id) ? 'checked' : ''} aria-label="Select crop">
    <span class="crop-media ${item.source_missing ? 'crop-missing' : ''}"><img src="${cropPreviewUrl(item)}" alt="${escapeHtml(item.current_label)} crop" loading="lazy"><i class="bi bi-image"></i>
      <span class="crop-label-pill">${escapeHtml(item.current_label)}</span>
      ${item.source_missing ? '<span class="badge-danger crop-missing-badge">Not in bucket</span>' : ''}
      <button type="button" class="crop-open-source" data-open-crop-source="${item.id}" aria-label="Open source image" title="Open source image"><i class="bi bi-arrows-angle-expand"></i></button>
    </span>
    <span class="crop-card-meta"><strong><i></i>${escapeHtml(item.current_label)}</strong><small>${escapeHtml(item.source_label)} · ${escapeHtml(item.region || '')}</small></span>
  </article>`).join('') : failed
    ? emptyStateHtml('bi-exclamation-triangle', 'Crops could not be loaded', 'The request failed. Check the connection and try again.', 'retry-crops', 'Try again')
    : emptyStateHtml('bi-grid-3x3-gap', 'No crops match', 'Nothing in this project fits the current filters.', 'clear-crop-filters', 'Clear filters')
  document.getElementById('clear-crop-filters')?.addEventListener('click', clearFilters)
  document.getElementById('retry-crops')?.addEventListener('click', () => loadCrops(false).catch(error => showNotice(error.message)))
  // A source that is missing may still have a stored crop; when the preview 404s the placeholder icon takes over.
  grid.querySelectorAll('.crop-media img').forEach(img => img.addEventListener('error', () => img.classList.add('d-none'), { once: true }))
  document.querySelectorAll('[data-open-crop-source]').forEach(button => button.addEventListener('click', async event => {
    event.stopPropagation()
    await openSource(Number(button.dataset.openCropSource), 'crop')
  }))
  document.querySelectorAll('[data-crop-id]').forEach(button => button.addEventListener('click', async () => {
    if (Date.now() < state.suppressGridClickUntil || state.lassoGrid === 'crop') return
    const itemId = Number(button.dataset.cropId)
    await openSource(itemId, 'crop')
  }))
  document.querySelectorAll('[data-crop-id]').forEach(card => card.addEventListener('keydown', event => {
    if (event.target === card && event.key === 'Enter') card.click()
  }))
  document.querySelectorAll('[data-select-crop]').forEach(checkbox => checkbox.addEventListener('click', event => {
    event.stopPropagation()
    const itemId = Number(checkbox.dataset.selectCrop)
    if (checkbox.checked) state.selectedCropIds.add(itemId)
    else state.selectedCropIds.delete(itemId)
    checkbox.closest('.curation-crop-card').classList.toggle('selected', checkbox.checked)
    updateBulkControls()
  }))
  document.querySelectorAll('.crop-media img').forEach(image => image.addEventListener('error', () => { image.classList.add('d-none'); image.closest('.crop-media')?.classList.add('crop-missing') }))
  setupGridKeyboard('crop-grid', state.selectedCropIds, updateBulkControls)
  setupGridLasso('crop-grid', 'crop', state.selectedCropIds, updateBulkControls)
  updateBulkControls()
}

function updateBulkControls() {
  const count = state.selectedCropIds.size
  const action = byId('bulk-action').value
  const relabel = action === 'relabel'
  byId('crop-selection-count').textContent = `${count} selected`
  byId('crop-selection-bar').classList.toggle('d-none', !count)
  byId('bulk-label').classList.toggle('d-none', !relabel)
  const label = relabel ? validRelabelCode(byId('bulk-label').value) : null
  byId('bulk-label').classList.toggle('is-invalid', relabel && !!byId('bulk-label').value.trim() && !label)
  byId('apply-bulk').disabled = !canDo('project-curator') || !count || !action || (relabel && !label)
  byId('apply-bulk').textContent = relabel && label && count ? `Relabel ${count} to ${label}` : 'Apply'
}

function reportConflicts(result) {
  if (!result?.conflicts?.length) return
  const names = [...new Set(result.conflicts.map(conflict => conflict.updated_by_name).filter(Boolean))]
  showNotice(`${result.updated} updated. ${result.conflicts.length} skipped because ${names.length ? names.join(', ') : 'someone else'} changed them after you loaded this page — they now show the latest state.`)
}

async function applyBulkAction() {
  const action = byId('bulk-action').value
  if (!action || !state.selectedCropIds.size) return
  // Send the revision each tile was loaded at, so a tile someone else has
  // changed since is left alone and reported rather than overwritten.
  const revisions = new Map(state.crops.map(item => [item.id, item.revision]))
  const payload = {
    items: [...state.selectedCropIds].map(id => ({ id, expected_revision: revisions.get(id) ?? null })),
    reason: byId('bulk-reason').value.trim() || 'Crop grid bulk action',
  }
  if (action === 'relabel') {
    const label = validRelabelCode(byId('bulk-label').value)
    if (!label) { showNotice('Choose a label code from the list'); return }
    payload.current_label = label
  } else if (action === 'include') payload.included = true
  else if (action === 'exclude') payload.included = false
  else payload.review_state = action
  const result = await request(`/api/curation/projects/${state.projectId}/items/bulk`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  })
  reportConflicts(result)
  state.selectedCropIds.clear()
  byId('bulk-reason').value = ''
  await Promise.all([loadCrops(false), loadStats(), loadFacets()])
}

// ── Links to a view ──────────────────────────────────────────────────────────
// The hash keeps project, view and evaluation run, so a refresh or a shared
// link lands where it was: /curation#project=<id>&view=versions
const VIEWS = ['source', 'crop', 'quality', 'status', 'matrix', 'versions', 'activity']

function readHash() {
  const params = new URLSearchParams(window.location.hash.slice(1))
  const view = params.get('view')
  const number = key => { const value = Number(params.get(key)); return Number.isInteger(value) && value > 0 ? value : null }
  return { project: params.get('project'), view: VIEWS.includes(view) ? view : null, run: params.get('run'), image: number('image'), item: number('item') }
}

function writeHash() {
  if (!state.projectId) return
  const params = new URLSearchParams({ project: state.projectId, view: state.view })
  const run = state.view === 'matrix' ? selectedRunId() : null
  if (run) params.set('run', run)
  if (state.view === 'source' && state.currentImage) {
    params.set('image', state.currentImage.first_item_id)
    if (state.selected) params.set('item', state.selected.id)
  }
  const next = `#${params}`
  if (window.location.hash !== next) history.replaceState(null, '', next)
}

// ── Navigation: where the source view was opened from, and how to get back ──
const BACK_LABELS = { crop: 'crop grid', quality: 'quality review', matrix: 'confusion matrix', versions: 'history', compare: 'compare', status: 'dataset status' }

// A Back target for the view being left. "parent" is the target that was already set when this view was itself
// reached from somewhere (matrix or history to the crop grid, then on to a source image), so Back unwinds one step at a time.
function makeBackTarget(from) {
  const panel = scrollPanel()
  return { view: from === 'compare' ? 'versions' : from, label: BACK_LABELS[from] || from, scrollTop: panel ? panel.scrollTop : 0, parent: state.view === from || state.view === 'crop' ? state.backTo : null }
}

// The element that scrolls the view panels: the page itself unless a container is made to scroll.
function scrollPanel() {
  const main = document.querySelector('.curation-main')
  return main && main.scrollHeight > main.clientHeight ? main : document.scrollingElement
}

async function openSource(itemId, from = state.view) {
  if (from && from !== 'source') state.backTo = makeBackTarget(from)
  setView('source')
  await selectImage(itemId, itemId)
}

function renderBackBar() {
  // The source view, and the crop grid when it was opened from the matrix, History or Compare.
  const show = (state.view === 'source' || state.view === 'crop') && Boolean(state.backTo)
  byId('back-bar').classList.toggle('d-none', !show)
  if (show) byId('back-label').textContent = `Back to ${state.backTo.label}`
  if (typeof fitWorkspaceToViewport === 'function') fitWorkspaceToViewport()
}

byId('back-button').addEventListener('click', async () => {
  const back = state.backTo
  if (!back) return
  // setView resolves once the view has loaded its data, or false when the unsaved-edits guard keeps us here
  // (back.view is never 'source', so it always replaces state.backTo itself when it does move).
  const shown = await setView(back.view, back.parent || null)
  if (!shown || state.view !== back.view) return  // stayed: unsaved edits, hash and Back bar all kept
  // Restore the scroll position once the reloaded view has rendered.
  requestAnimationFrame(() => {
    const panel = scrollPanel()
    if (panel) panel.scrollTop = back.scrollTop
  })
})

// Open the image and point a link names, once its project has loaded.
async function applyLinkedImage(linked) {
  if (!linked || linked.view !== 'source' || !linked.image || !state.projectId) return
  try { await selectImage(linked.image, linked.item || linked.image) } catch (error) { showNotice(error.message) }
}

// Resolves true once the view has loaded its data (a failed load still says why), or false when the
// unsaved-edits guard keeps the source view (state, hash and Back bar untouched). With no unsaved edits
// nothing is awaited before the switch, so the view changes synchronously as callers have always seen it.
async function setView(view, backTarget = null) {
  if (state.view === 'source' && view !== 'source' && typeof pendingCount === 'function' && pendingCount()
    && typeof confirmDiscardPending === 'function' && !(await confirmDiscardPending())) { writeHash(); return false }
  state.view = view
  renderFilterChipBar()
  renderMatrixDrilldown()
  if (typeof syncClaims === 'function') syncClaims()  // live.js: leaving a view lets go of what it had open
  if (typeof sendPresence === 'function' && live.projectId === state.projectId) sendPresence()  // others see the move now
  if (view !== 'source') state.backTo = backTarget
  const source = view === 'source'
  const crops = view === 'crop'
  const quality = view === 'quality'
  ;[['source', 'source-view-button'], ['crop', 'crop-view-button'], ['quality', 'quality-view-button'],
    ['status', 'status-view-button'], ['matrix', 'matrix-view-button'], ['versions', 'versions-view-button'], ['activity', 'activity-view-button']].forEach(([name, id]) => {
    const tab = byId(id)
    tab.classList.toggle('active', view === name)
    if (view === name) tab.setAttribute('aria-current', 'true'); else tab.removeAttribute('aria-current')
  })
  byId('versions-panel').classList.toggle('d-none', view !== 'versions' || !state.projectId)
  byId('activity-panel').classList.toggle('d-none', view !== 'activity' || !state.projectId)
  byId('curation-workspace').classList.toggle('d-none', !source || !state.projectId)
  byId('crop-grid-panel').classList.toggle('d-none', !crops || !state.projectId)
  byId('quality-panel').classList.toggle('d-none', !quality || !state.projectId)
  byId('dataset-status-panel').classList.toggle('d-none', view !== 'status' || !state.projectId)
  byId('confusion-matrix-panel').classList.toggle('d-none', view !== 'matrix' || !state.projectId)
  byId('annotation-table-panel').classList.toggle('d-none', !source || !state.currentImage)
  const loads = []
  // The first visit loads the queue and opens its first image, so there is something to review straight away.
  if (source && (state.matrixFilter || !state.sourceQueueLoaded)) {
    loads.push(loadQueue(false).then(() => {
      if (!state.currentImage && !state.pendingImage && state.images[0]) {
        return selectImage(state.images[0].first_item_id)
          .then(opened => opened && typeof skipHeldImages === 'function' && skipHeldImages(1))
      }
    }))
  }
  if (crops) loads.push(loadCrops(false))
  if (quality) loads.push(loadQualityReview())
  if (view === 'status') loads.push(loadDatasetStatus())
  if (view === 'matrix') loads.push(loadConfusionMatrix())
  if (view === 'versions') loads.push(loadVersions())
  if (view === 'activity' && typeof loadActivityView === 'function') loads.push(loadActivityView())
  renderBackBar()
  writeHash()
  await Promise.all(loads.map(load => Promise.resolve(load).catch(error => showNotice(error.message))))
  return true
}

// ── Dataset versions ─────────────────────────────────────────────────────────
// What publishing now would put in and take out, and the versions so far.

let versionsTimer = null
const fmt = value => Number(value || 0).toLocaleString()

async function loadVersions() {
  clearTimeout(versionsTimer)
  if (!state.projectId) return
  const projectId = state.projectId
  const [preview, list] = await Promise.all([
    request(`/api/curation/projects/${projectId}/versions/preview`),
    request(`/api/curation/projects/${projectId}/versions`),
  ])
  if (projectId !== state.projectId) return
  renderPreview(preview)
  renderVersionList(list.versions)
  if (typeof loadHistory === 'function') loadHistory().catch(error => showNotice(error.message))
  // Keep polling while something is being built, so the page updates itself.
  const busy = list.versions.some(v => v.status === 'building' || v.images?.status === 'running')
  if (busy && state.view === 'versions') versionsTimer = setTimeout(loadVersions, 3000)
}

function renderPreview(preview) {
  const c = preview.changes
  byId('preview-title').textContent = preview.base_version
    ? `What publishing v${preview.base_version + 1} would change`
    : 'What the first version would contain'
  byId('preview-subtitle').textContent = preview.base_version
    ? `Compared with v${preview.base_version}${preview.unassigned_split ? ` · ${fmt(preview.unassigned_split)} included items get a train/val/test split on publish` : ''}`
    : 'Nothing has been published yet'
  byId('preview-kpis').innerHTML = [
    ['In v' + (preview.base_version || '—'), fmt(c.before)], ['Coming in', `+${fmt(c.in)}`], ['Going out', `−${fmt(c.out)}`],
    ['Relabelled', fmt(c.relabelled)], ['Changed split', fmt(c.moved || 0)], ['Would be', fmt(c.after)],
  ].map(([label, value]) => `<div><strong>${value}</strong><span>${label}</span></div>`).join('')
  byId('preview-classes').innerHTML = c.per_class.length
    ? c.per_class.slice(0, 200).map(row => `<tr>
        <td><i class="legend-dot" style="background:${labelColor(row.label)}"></i> <strong>${escapeHtml(row.label)}</strong></td>
        <td class="num">${fmt(row.before)}</td>
        <td class="num ${row.in ? 'delta-in' : ''}">${row.in ? '+' + fmt(row.in) : ''}</td>
        <td class="num ${row.out ? 'delta-out' : ''}">${row.out ? '−' + fmt(row.out) : ''}</td>
        <td class="num">${row.relabelled_in ? fmt(row.relabelled_in) : ''}</td>
        <td class="num">${row.relabelled_out ? fmt(row.relabelled_out) : ''}</td>
        <td class="num">${row.moved ? fmt(row.moved) : ''}</td>
        <td class="num">${fmt(row.after)}</td>
        <td class="num ${row.net > 0 ? 'delta-in' : row.net < 0 ? 'delta-out' : ''}">${row.net > 0 ? '+' : ''}${row.net ? fmt(row.net) : '0'}</td>
      </tr>`).join('')
    : '<tr><td colspan="9" class="text-muted">No changes since the last version.</td></tr>'
  byId('preview-contributors').innerHTML = preview.contributors.length
    ? preview.contributors.map(person => `<div class="status-row"><span><b>${escapeHtml(person.name)}</b></span>
        <span class="small">${person.classes.map(cls => `${escapeHtml(cls.label)} ${fmt(cls.items)}`).join(' · ')}</span>
        <span>${fmt(person.changes)} changes · ${fmt(person.items)} items</span></div>`).join('')
    : '<p class="text-muted small">No review changes since then.</p>'
  const sets = (preview.change_sets || []).map(cs => `${escapeHtml(cs.author)}: ${escapeHtml(cs.message)} (${fmt(cs.change_count)} changes)`)
  const open = (preview.uncommitted || []).map(u => `${escapeHtml(u.name)} ${fmt(u.count)}`)
  const extra = [sets.length ? `<div class="small mt-2"><b>Change sets:</b> ${sets.join(' · ')}</div>` : '',
    open.length ? `<div class="small text-muted">Uncommitted: ${open.join(' · ')}</div>` : ''].join('')
  if (extra) byId('preview-contributors').insertAdjacentHTML('beforeend', extra)
}

function renderVersionList(list) {
  const published = list.filter(v => v.status === 'published').map(v => v.version)
  const options = [...published.map(n => `<option value="${n}">v${n}</option>`), '<option value="live">Live draft</option>'].join('')
  for (const [id, fallback] of [['compare-base', published[1] ?? published[0]], ['compare-target', published[0] !== undefined && published.length > 1 ? published[0] : 'live']]) {
    const select = byId(id)
    const keep = select.value
    select.innerHTML = options
    select.value = [...select.options].some(o => o.value === keep) ? keep : String(fallback ?? 'live')
  }
  byId('compare-run').disabled = !published.length
  if (state.compareProject !== state.projectId) {  // another project's comparison does not belong here
    state.compareProject = state.projectId
    byId('compare-result').innerHTML = ''
  }
  byId('versions-count').textContent = list.length ? `${list.length} version${list.length === 1 ? '' : 's'}` : 'none yet'
  if (!list.length) {
    byId('versions-list').innerHTML = '<div class="empty-state"><i class="bi bi-box-seam" aria-hidden="true"></i><strong>No versions yet</strong><p>Publish the first version to freeze what has been curated so far. Each later version records what came in, what went out and who changed it.</p></div>'
    return
  }
  const base = `/api/curation/projects/${state.projectId}/versions`
  byId('versions-list').innerHTML = list.map(v => {
    const c = v.changes || {}
    const splits = v.statistics?.splits || {}
    const images = v.images
    const imageState = !images ? ''
      : images.status === 'running' ? `<span class="small">writing images ${fmt(images.done)} / ${fmt(images.total)}${images.failed ? ` (${fmt(images.failed)} failed)` : ''}…</span>`
        : images.status === 'completed' ? `<span class="small">images: ${fmt(images.done)} written${images.failed ? `, ${fmt(images.failed)} missing` : ''} → <code>${escapeHtml(images.location || '')}</code></span>`
          : `<span class="small text-danger">images ${escapeHtml(images.status)}${images.error ? `: ${escapeHtml(images.error)}` : ''}</span>`
    return `<article class="version-card">
      <header><strong>v${v.version}</strong><span class="badge version-status ${escapeHtml(v.status)}">${escapeHtml(v.status)}</span>
        <span class="version-meta">${escapeHtml(v.created_by_name || '')} · ${escapeHtml(timeAgo(v.published_at || v.created_at))}</span></header>
      ${v.status === 'published' ? `<div class="version-delta"><b>${fmt(v.item_count)}</b> items (train ${fmt(splits.train)} · val ${fmt(splits.val)} · test ${fmt(splits.test)}) ·
        <span class="delta-in">+${fmt(c.in)}</span> <span class="delta-out">−${fmt(c.out)}</span> · ${fmt(c.relabelled)} relabelled
        ${c.previous_version ? `since v${c.previous_version}` : ''} · ${fmt(v.statistics?.class_count)} classes</div>` : ''}
      ${v.error_message ? `<div class="small text-danger">${escapeHtml(v.error_message)}</div>` : ''}
      ${v.notes ? `<blockquote>${escapeHtml(v.notes)}</blockquote>` : ''}
      ${v.contributors?.length ? `<div class="version-meta">Contributors: ${v.contributors.map(p => `${escapeHtml(p.name)} (${fmt(p.items)})`).join(', ')}</div>` : ''}
      ${v.status === 'published' ? `<div class="version-links">
        <a class="btn btn-sm btn-outline-secondary" href="${base}/${v.version}/files/CHANGELOG.md"><i class="bi bi-journal-text"></i> Change log</a>
        <a class="btn btn-sm btn-outline-secondary" href="${base}/${v.version}/files/manifest.csv"><i class="bi bi-filetype-csv"></i> Manifest</a>
        <a class="btn btn-sm btn-outline-secondary" href="${base}/${v.version}/files/manifest.json"><i class="bi bi-filetype-json"></i> JSON</a>
        ${!images || ['failed', 'interrupted'].includes(images.status) ? `<button type="button" class="btn btn-sm btn-outline-primary" data-export-images="${v.version}" ${canDo('project-lead') ? '' : 'disabled'} title="Write train/val/test class folders of crops — copies every crop, so it is slow on a big version"><i class="bi bi-folder2-open"></i> Write image folders</button>` : ''}
        <button type="button" class="btn btn-sm btn-outline-secondary" data-version-breakdown="${v.version}"><i class="bi bi-table"></i> Breakdown</button>
        ${imageState}
      </div>` : ''}
      ${v.status === 'published' ? `<div class="version-meta">Leakage: ${fmt(v.statistics?.cross_split_source_images)} source images span more than one split · split origins: ${Object.entries(v.statistics?.split_origins || {}).map(([origin, count]) => `${escapeHtml(origin)} ${fmt(count)}`).join(', ') || '—'}</div>
      <div class="version-breakdown d-none" data-version-breakdown-body="${v.version}"></div>` : ''}
    </article>`
  }).join('')
  document.querySelectorAll('[data-export-images]').forEach(button => button.addEventListener('click', async () => {
    const number = button.dataset.exportImages
    const version = list.find(v => String(v.version) === number)
    const ok = await window.OF.ui.confirm({
      title: `Write ${fmt(version?.item_count)} crops into train/val/test folders for v${number}?`,
      message: 'This copies every crop and can take a long time on a large version.',
      confirmLabel: 'Write image folders',
    })
    if (!ok) return
    await request(`${base}/${number}/images`, { method: 'POST' })
    loadVersions()
  }))
  document.querySelectorAll('[data-version-breakdown]').forEach(button => button.addEventListener('click', async () => {
    const number = button.dataset.versionBreakdown
    const body = document.querySelector(`[data-version-breakdown-body="${number}"]`)
    const opening = body.classList.contains('d-none')
    body.classList.toggle('d-none', !opening)
    if (!opening || body.dataset.loaded) return
    body.innerHTML = '<span class="small text-muted">Loading…</span>'
    try {
      renderBreakdownTable(body, await request(`${base}/${number}/breakdown?by=label`))
      body.dataset.loaded = '1'
    } catch (error) { body.innerHTML = `<span class="small text-danger">${escapeHtml(error.message)}</span>` }
  }))
}

async function runCompare() {
  const base = byId('compare-base').value
  const target = byId('compare-target').value
  const box = byId('compare-result')
  if (base === target) { box.innerHTML = '<span class="small text-muted">Choose two different versions.</span>'; return }
  box.innerHTML = '<span class="small text-muted">Comparing…</span>'
  try {
    renderCompare(await request(`/api/curation/projects/${state.projectId}/versions/compare?base=${encodeURIComponent(base)}&target=${encodeURIComponent(target)}`))
  } catch (error) { box.innerHTML = `<span class="small text-danger">${escapeHtml(error.message)}</span>` }
}

// An unassigned item gets its split on publish, so say that rather than showing a bare name.
const splitName = split => split === 'unassigned' ? 'unassigned (gets a split on publish)' : split

function renderCompare(result) {
  const c = result.changes
  const name = value => value === 'live' ? 'live' : value
  byId('compare-result').innerHTML = `
    <div class="status-kpis">${[[`In ${name(result.base)}`, fmt(c.before)], ['Came in', `+${fmt(c.in)}`], ['Went out', `−${fmt(c.out)}`],
      ['Relabelled', fmt(c.relabelled)], ['Changed split', fmt(c.moved)], [`In ${name(result.target)}`, fmt(c.after)]]
      .map(([label, value]) => `<div><strong>${value}</strong><span>${escapeHtml(label)}</span></div>`).join('')}</div>
    ${c.split_moves.length ? `<p class="small mb-1">Split moves: ${c.split_moves.map(m => `${escapeHtml(splitName(m.from))} → ${escapeHtml(splitName(m.to))} ${fmt(m.count)}`).join(' · ')}</p>` : ''}
    <div class="table-responsive versions-table-wrap"><table class="table table-sm align-middle mb-0">
      <thead><tr><th>Class</th><th class="num">Before</th><th class="num">In</th><th class="num">Out</th><th class="num">Relabelled in</th><th class="num">Relabelled out</th><th class="num">Moved split</th><th class="num">After</th><th class="num">Net</th></tr></thead>
      <tbody>${c.per_class.length ? c.per_class.map(row => `<tr>
        <td><a href="#" data-compare-class="${escapeHtml(row.label)}"><i class="legend-dot" style="background:${labelColor(row.label)}"></i> <strong>${escapeHtml(row.label)}</strong></a></td>
        <td class="num">${fmt(row.before)}</td><td class="num delta-in">${row.in ? '+' + fmt(row.in) : ''}</td>
        <td class="num delta-out">${row.out ? '−' + fmt(row.out) : ''}</td>
        <td class="num">${row.relabelled_in ? fmt(row.relabelled_in) : ''}</td><td class="num">${row.relabelled_out ? fmt(row.relabelled_out) : ''}</td>
        <td class="num" title="${escapeHtml(Object.entries(row.moved_to || {}).map(([s, n]) => `${n} to ${splitName(s)}`).join(', '))}">${row.moved ? fmt(row.moved) : ''}</td>
        <td class="num">${fmt(row.after)}</td>
        <td class="num ${row.net > 0 ? 'delta-in' : row.net < 0 ? 'delta-out' : ''}">${row.net > 0 ? '+' : ''}${row.net ? fmt(row.net) : '0'}</td>
      </tr>`).join('') : '<tr><td colspan="9" class="text-muted">No differences.</td></tr>'}</tbody></table></div>
    ${c.relabels.length ? `<p class="small mt-2 mb-0">Top relabels: ${c.relabels.slice(0, 10).map(r => `${escapeHtml(r.from)} → ${escapeHtml(r.to)} ${fmt(r.count)}`).join(' · ')}</p>` : ''}`
  byId('compare-result').querySelectorAll('[data-compare-class]').forEach(link => link.addEventListener('click', event => {
    event.preventDefault()
    // Replace, not add to, whatever code filters were set before.
    state.selectedCodes.clear()
    state.selectedTaxonomy.clear()
    byId('taxonomy-filter').value = ''
    byId('filter-label').value = link.dataset.compareClass
    addCodeFilters()
    setView('crop', makeBackTarget('compare'))
  }))
}

byId('compare-run').addEventListener('click', runCompare)

async function publishVersion(event) {
  event.preventDefault()
  const message = byId('publish-message')
  message.textContent = ''
  try {
    const version = await request(`/api/curation/projects/${state.projectId}/versions`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notes: byId('publish-notes').value.trim() || null }),
    })
    byId('publish-notes').value = ''
    message.textContent = `Publishing v${version.version}…`
    message.className = 'small text-success'
    await loadVersions()
  } catch (error) {
    message.textContent = error.message
    message.className = 'small text-danger'
  }
}

function scoreImageQuality(img) {
  if (!img || !img.naturalWidth || !img.naturalHeight) {
    return { score: 0, flags: ['analysis_failed'], decision: 'manual_review', metrics: null }
  }
  const canvas = document.createElement('canvas')
  const context = canvas.getContext('2d')
  canvas.width = Math.min(128, img.naturalWidth)
  canvas.height = Math.min(128, img.naturalHeight)
  context.drawImage(img, 0, 0, canvas.width, canvas.height)
  const data = context.getImageData(0, 0, canvas.width, canvas.height).data
  let brightnessTotal = 0
  let blueTotal = 0
  let redTotal = 0
  let greenTotal = 0
  let darkPixels = 0
  let brightPixels = 0
  const luminance = new Float32Array(data.length / 4)
  for (let index = 0; index < data.length; index += 4) {
    const red = data[index]
    const green = data[index + 1]
    const blue = data[index + 2]
    const value = 0.299 * red + 0.587 * green + 0.114 * blue
    luminance[index / 4] = value
    brightnessTotal += value
    blueTotal += blue
    redTotal += red
    greenTotal += green
    if (value < 20) darkPixels += 1
    if (value > 245) brightPixels += 1
  }
  const pixelCount = Math.max(1, data.length / 4)
  const meanBrightness = brightnessTotal / pixelCount
  const contrast = Math.sqrt(luminance.reduce((sum, value) => sum + (value - meanBrightness) ** 2, 0) / pixelCount)
  let laplacianTotal = 0
  let laplacianSquaredTotal = 0
  let laplacianCount = 0
  for (let row = 1; row < canvas.height - 1; row += 1) {
    for (let column = 1; column < canvas.width - 1; column += 1) {
      const offset = row * canvas.width + column
      const value = 4 * luminance[offset] - luminance[offset - 1] - luminance[offset + 1] - luminance[offset - canvas.width] - luminance[offset + canvas.width]
      laplacianTotal += value
      laplacianSquaredTotal += value * value
      laplacianCount += 1
    }
  }
  const laplacianMean = laplacianTotal / Math.max(1, laplacianCount)
  const sharpness = Math.max(0, laplacianSquaredTotal / Math.max(1, laplacianCount) - laplacianMean ** 2)
  const channels = [redTotal / pixelCount, greenTotal / pixelCount, blueTotal / pixelCount]
  const colorCast = Math.max(...channels) / Math.max(1, Math.min(...channels))
  const blueBias = channels[2] / Math.max(1, channels[0] + channels[1] + channels[2])
  const clipped = (darkPixels + brightPixels) / pixelCount
  let score = 100
  const flags = []
  if (sharpness < 20) {
    score -= 32
    flags.push('blurry')
  } else if (sharpness < 45) {
    score -= 14
    flags.push('soft')
  }
  if (meanBrightness < 55) {
    score -= 28
    flags.push('too_dark')
  } else if (meanBrightness > 205) {
    score -= 30
    flags.push('too_bright')
  }
  if (contrast < 28) {
    score -= 18
    flags.push('low_contrast')
  }
  if (colorCast > 1.65 && blueBias > 0.38) {
    score -= 18
    flags.push('blue_cast')
  } else if (colorCast > 1.8) {
    score -= 12
    flags.push('color_cast')
  }
  if (brightPixels / pixelCount > 0.15) {
    score -= 20
    flags.push('washed_out')
  }
  const uniqueFlags = [...new Set(flags)]
  score = Math.round(Math.max(0, Math.min(100, score)))
  let decision = 'accept'
  if (score < 55 || uniqueFlags.length >= 2) decision = 'manual_review'
  if (score < 35 || uniqueFlags.includes('washed_out') && uniqueFlags.includes('too_bright')) decision = 'reject'
  return {
    score,
    flags: uniqueFlags.length ? uniqueFlags : ['clean'],
    decision,
    metrics: {
      sharpness: Math.round(sharpness),
      brightness: Math.round(meanBrightness),
      contrast: Math.round(contrast),
      colorCast: Number(colorCast.toFixed(2)),
      clipped: Math.round(clipped * 100),
    },
  }
}

function updateQualityProgress(completed, total, label = 'Analyzing image quality') {
  const percent = total ? Math.round(completed / total * 100) : 0
  byId('quality-progress-label').textContent = label
  byId('quality-progress-count').textContent = `${completed} / ${total}`
  byId('quality-progress-bar').style.width = `${percent}%`
}

// One page at a time (keyset, like the crop grid): Load more appends the next page and analyses only it.
async function loadQualityReview(append = false) {
  if (!state.projectId) return
  if (append && !state.qualityCursor) return
  const runId = ++state.qualityRunId
  const patchScope = state.qualityScope === 'patch'
  const kept = append ? state.qualityItems : []
  if (!append) {
    state.qualityItems = []
    state.qualityCursor = null
    state.qualityTotal = null
    byId('quality-summary').innerHTML = ''
    byId('quality-list').innerHTML = skeletonRows(6).replaceAll('skeleton-row', 'skeleton-row skeleton-card')  // replaced as results arrive
  }
  byId('quality-load-more').disabled = true
  updateQualityProgress(0, 0, patchScope ? 'Finding patches…' : 'Finding source images…')
  if (!append) await loadQualityPrep().catch(() => {})
  const params = currentFilters()
  params.set('limit', patchScope ? '200' : '50')
  if (patchScope && !append) params.set('with_total', 'true')
  // Once the server has scored the project, the signal filter runs there, over every patch.
  const signal = byId('quality-signal-filter').value
  if (patchScope && state.qualityPrepared && signal) {
    params.set(signal === 'attention' ? 'quality_suggestion' : 'quality_flag', signal)
  }
  if (append) {
    params.set('after_id', state.qualityCursor.id)
    if (!patchScope) params.set('after_missing', state.qualityCursor.missing)
  }
  try {
    const endpoint = patchScope ? 'items' : 'source-images'
    const page = await request(`/api/curation/projects/${state.projectId}/${endpoint}?${params}`)
    if (runId !== state.qualityRunId) return
    const entries = patchScope ? page.items || [] : page.images || []
    state.qualityCursor = page.next_cursor ? { id: page.next_cursor, missing: page.next_missing || 0 } : null
    if (page.total != null) state.qualityTotal = page.total
    // Scores the server already has (Prepare quality scores): those patches are not analysed here.
    let stored = {}
    if (patchScope && entries.length) {
      try {
        stored = (await request(`/api/curation/projects/${state.projectId}/quality-scores?item_ids=${entries.map(entry => entry.id).join(',')}`)).scores || {}
      } catch (_) { stored = {} }
      if (runId !== state.qualityRunId) return
    }
    const results = new Array(entries.length)
    let nextIndex = 0
    let completed = 0
    updateQualityProgress(0, entries.length)
    const analyzeNext = async () => {
      while (nextIndex < entries.length && runId === state.qualityRunId) {
        const index = nextIndex++
        const entry = entries[index]
        try {
          const detail = patchScope
            ? entry
            : await request(`/api/curation/projects/${state.projectId}/source-images/${entry.first_item_id}`)
          const review = patchScope
            ? detail.metadata?.patch_quality_review
            : detail.items.find(item => item.metadata?.quality_review)?.metadata?.quality_review
          const previewUrl = patchScope
            ? cropPreviewUrl(detail)
            : proxyUrl(detail.source_image_uri)
          const saved = patchScope ? stored[String(entry.id)] : null
          let quality
          if (saved) {
            quality = { score: saved.score, flags: saved.flags, decision: saved.suggestion, metrics: saved.metrics, server: true }
          } else {
            const preview = new Image()
            preview.src = previewUrl
            await new Promise(resolve => { preview.onload = resolve; preview.onerror = resolve })
            quality = scoreImageQuality(preview)
          }
          results[index] = {
            ...entry,
            reviewId: patchScope ? detail.id : detail.first_item_id,
            qualityScope: state.qualityScope,
            previewUrl,
            source_image_uri: detail.source_image_uri,
            image_name: patchScope ? detail.file_name : detail.image_name,
            itemLabel: patchScope ? detail.current_label : null,
            quality,
            savedDecision: review?.decision || null,
            reviewedBy: review?.reviewed_by || null,
            included: patchScope ? detail.included : detail.items.some(item => item.included),
            revision: patchScope ? detail.revision : entry.revision,
          }
        } catch (error) {
          results[index] = { ...entry, reviewId: patchScope ? entry.id : entry.first_item_id, qualityScope: state.qualityScope, quality: scoreImageQuality(null), analysisError: error.message, included: entry.included ?? null }
        }
        completed += 1
        updateQualityProgress(completed, entries.length)
      }
    }
    await Promise.all(Array.from({ length: Math.min(4, entries.length) }, analyzeNext))
    if (runId !== state.qualityRunId) return
    state.qualityItems = [...kept, ...results.filter(Boolean)]
    const noun = patchScope ? 'patches' : 'source images'
    const loaded = state.qualityItems.length
    updateQualityProgress(completed, entries.length, loaded ? `${patchScope ? 'Patch' : 'Source image'} analysis complete` : `No ${noun} match these filters`)
    renderQualitySummary(state.qualityItems)
    renderQualityList()
    renderQualityPaging(noun)
    if (typeof syncClaims === 'function') syncClaims()  // live.js: the undecided patches loaded here
  } catch (error) {
    if (runId !== state.qualityRunId) return
    updateQualityProgress(0, 0, 'Analysis could not start')
    if (append) { renderQualityPaging(patchScope ? 'patches' : 'source images'); throw error }
    byId('quality-list').innerHTML = `<div class="quality-empty empty-state"><i class="bi bi-exclamation-triangle" aria-hidden="true"></i><strong>Quality analysis failed</strong><p>${escapeHtml(error.message)}</p></div>`
  }
}

// "Showing N of M" and Load more. Sorting and the signal filter run in the browser, on what is loaded.
function renderQualityPaging(noun) {
  const loaded = state.qualityItems.length
  const total = state.qualityTotal
  byId('quality-paging-count').textContent = loaded
    ? `Showing ${fmt(loaded)}${total != null ? ` of ${fmt(total)}` : state.qualityCursor ? '+' : ''} ${noun}${state.qualityCursor ? ' · sorted within the loaded ones' : ''}`
    : ''
  byId('quality-load-more').classList.toggle('d-none', !state.qualityCursor)
  byId('quality-load-more').disabled = false
}

// Server quality scores (curation/quality.py): how many patches are scored, and the lead's Prepare button.
let qualityPrepTimer = null

async function loadQualityPrep() {
  const pid = state.projectId
  clearTimeout(qualityPrepTimer)
  const data = await request(`/api/curation/projects/${pid}/quality-scores`)
  if (state.projectId !== pid) return
  state.qualityPrepared = data.scored > 0
  const job = data.job
  const running = job && ['queued', 'starting', 'running'].includes(job.status)
  const scored = data.scored ? `Quality scores prepared for ${fmt(data.scored)} of ${fmt(data.items)} patches` +
    `${data.average != null ? ` (average ${data.average})` : ''}; the signal filter covers all of them.` : ''
  byId('quality-prep-text').textContent = running
    ? `Preparing quality scores… ${fmt(job.processed || 0)} of ${fmt(job.total || data.items)} patches checked.`
    : scored || 'Scores are worked out in the browser for the loaded patches only. A project lead can prepare them once for every patch.'
  if (job && ['failed', 'interrupted'].includes(job.status) && !running) {
    byId('quality-prep-text').textContent += ` The last run ${job.status}${job.error ? `: ${job.error}` : ''}; Prepare again continues where it stopped.`
  }
  const button = byId('quality-prep-button')
  button.textContent = data.scored && data.scored < data.items ? 'Score the rest' : data.scored ? 'Score new or moved patches' : 'Prepare quality scores'
  button.disabled = running || !canDo('project-lead')
  if (running) qualityPrepTimer = setTimeout(() => { if (state.view === 'quality') loadQualityPrep().catch(() => {}) }, 4000)
}

byId('quality-prep-button').addEventListener('click', async () => {
  const button = byId('quality-prep-button')
  button.disabled = true
  try {
    await request(`/api/curation/projects/${state.projectId}/quality-scores`, { method: 'POST' })
    await loadQualityPrep()
  } catch (error) {
    button.disabled = false
    showNotice(error.message)
  }
})

function renderQualitySummary(items) {
  const total = items.length
  const clean = items.filter(item => item.quality.flags.includes('clean')).length
  const attention = items.filter(item => item.quality.decision !== 'accept' && !item.quality.flags.includes('analysis_failed')).length
  const failed = items.filter(item => item.quality.flags.includes('analysis_failed')).length
  const reviewed = items.filter(item => item.savedDecision).length
  const avg = total ? Math.round(items.reduce((sum, item) => sum + item.quality.score, 0) / total) : 0
  byId('quality-summary').innerHTML = [
    ['Analyzed', `${total}`], ['Average score', `${avg}`], ['No warnings', `${clean}`],
    ['Needs attention', `${attention}`], ['Analysis failed', `${failed}`], ['Human reviewed', `${reviewed}`],
  ].map(([label, value]) => `<div><strong>${value}</strong><span>${label}</span></div>`).join('')
}

const qualityFlagLabels = {
  clean: 'No threshold warnings', blurry: 'Low sharpness', soft: 'Slightly soft', too_dark: 'Too dark',
  too_bright: 'Too bright', low_contrast: 'Low contrast', blue_cast: 'Strong blue cast',
  color_cast: 'Strong color cast', washed_out: 'Clipped highlights', analysis_failed: 'Analysis failed',
}

const QUALITY_ACTIONS = [
  ['accept', 'btn-outline-success', 'bi-check-lg', 'Include'],
  ['manual_review', 'btn-outline-secondary', 'bi-pause-fill', 'Hold'],
  ['reject', 'btn-outline-danger', 'bi-x-lg', 'Exclude'],
]

function qualityBadges(item) {
  const membership = item.included === false
    ? '<span class="badge quality-badge quality-badge-out">Excluded</span>'
    : item.included === true ? '<span class="badge-ok quality-badge quality-badge-in">In dataset</span>' : ''
  const held = item.savedDecision === 'manual_review' ? '<span class="badge-warn quality-badge quality-badge-held">Held</span>' : ''
  const by = item.reviewedBy ? `<small class="text-muted">by ${escapeHtml(item.reviewedBy)}</small>` : ''
  return `${membership}${held}${by}`
}

function renderQualityList() {
  const list = byId('quality-list')
  const signal = byId('quality-signal-filter').value
  const sort = byId('quality-sort').value
  const items = state.qualityItems.filter(item => !signal ||
    (signal === 'attention' ? item.quality.decision !== 'accept' : item.quality.flags.includes(signal))).sort((left, right) => {
    if (sort === 'score_desc') return right.quality.score - left.quality.score
    if (sort === 'sharpness_asc') return (left.quality.metrics?.sharpness ?? -1) - (right.quality.metrics?.sharpness ?? -1)
    if (sort === 'brightness_asc') return (left.quality.metrics?.brightness ?? -1) - (right.quality.metrics?.brightness ?? -1)
    return left.quality.score - right.quality.score
  })
  if (!items.length) {
    list.innerHTML = '<div class="quality-empty empty-state"><i class="bi bi-funnel" aria-hidden="true"></i><strong>No matching results</strong><p>Try another decision or quality-signal filter.</p></div>'
    updateQualityBulkButtons()
    return
  }
  list.innerHTML = items.map(item => {
    const quality = item.quality || { score: 0, flags: ['unknown'], decision: 'manual_review' }
    const color = quality.score >= 75 ? 'var(--ok)' : quality.score >= 50 ? 'var(--warn)' : 'var(--danger)'
    const recommendation = quality.decision === 'accept' ? 'Looks usable' : quality.decision === 'reject' ? 'Recommend reject' : 'Review recommended'
    const metrics = quality.metrics
    const patchScope = item.qualityScope === 'patch'
    const context = patchScope ? `${escapeHtml(item.itemLabel || 'Unlabeled')} patch${item.split ? ` · ${escapeHtml(item.split)}` : ''}` : `${item.annotation_count || 0} annotation points`
    return `<article class="quality-card ${patchScope ? 'quality-card-patch' : ''}">
      <img src="${item.previewUrl || proxyUrl(item.source_image_uri)}" alt="${escapeHtml(item.image_name)} quality preview" loading="lazy">
      <div class="quality-card-body">
        <strong><span>${escapeHtml(item.image_name)}</span></strong>
        <small>${escapeHtml(item.site || item.region || 'Unknown site')} · ${context}</small>
        <div class="quality-verdict"><span class="badge quality-recommendation">${recommendation}</span>${qualityBadges(item)}</div>
        ${metrics ? `<dl class="quality-metrics">
          <div><dt>Sharpness</dt><dd>${metrics.sharpness}</dd></div><div><dt>Brightness</dt><dd>${metrics.brightness}/255</dd></div>
          <div><dt>Contrast</dt><dd>${metrics.contrast}</dd></div><div><dt>Color cast</dt><dd>${metrics.colorCast}x</dd></div>
          <div><dt>Clipped pixels</dt><dd>${metrics.clipped}%</dd></div>
        </dl>` : `<div class="quality-analysis-error">${escapeHtml(item.analysisError || 'The image could not be decoded for analysis.')}</div>`}
        <div class="quality-tags">${quality.flags.map(flag => `<span class="badge quality-tag quality-tag-${escapeHtml(flag)}">${escapeHtml(qualityFlagLabels[flag] || flag)}</span>`).join('')}</div>
        <div class="quality-score-block" style="--quality-score-color:${color}">
          <span>${quality.score}</span><small>/ 100 quality score</small>
        </div>
        <div class="quality-actions-row">
          ${QUALITY_ACTIONS.map(([action, style, icon, label]) => `<button type="button" class="btn btn-sm ${style} ${item.savedDecision === action ? 'active' : ''}" data-quality-action="${action}" data-quality-id="${item.reviewId}" ${canDo('project-curator') ? '' : 'disabled'}><i class="bi ${icon}"></i> ${label}</button>`).join('')}
          <button type="button" class="btn btn-sm btn-outline-secondary" data-open-source="${item.reviewId}"><i class="bi bi-image"></i> Source image</button>
        </div>
      </div>
    </article>`
  }).join('')
  document.querySelectorAll('#quality-list [data-open-source]').forEach(button => button.addEventListener('click', () => openSource(Number(button.dataset.openSource), 'quality')))
  document.querySelectorAll('[data-quality-action]').forEach(button => button.addEventListener('click', async () => {
    const reviewId = Number(button.dataset.qualityId)
    const item = state.qualityItems.find(candidate => candidate.reviewId === reviewId)
    if (!item) return
    const saved = await request(qualityReviewEndpoint(item), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision: button.dataset.qualityAction, score: item.quality.score, flags: item.quality.flags || [], expected_revision: item.revision ?? null }),
    })
    item.savedDecision = saved.quality_decision
    item.included = saved.included
    item.revision = saved.revision
    item.reviewedBy = saved.quality_reviewed_by
    renderQualityList()
    loadStats()
  }))
  updateQualityBulkButtons()
}

byId('quality-view-button').addEventListener('click', () => setView('quality'))

function qualityReviewEndpoint(item) {
  const segment = item.qualityScope === 'patch' ? `items/${item.reviewId}` : `source-images/${item.reviewId}`
  return `/api/curation/projects/${state.projectId}/${segment}/quality`
}

function setQualityScope(scope) {
  state.qualityScope = scope
  document.querySelectorAll('[data-quality-scope]').forEach(button => {
    const active = button.dataset.qualityScope === scope
    button.classList.toggle('active', active)
    button.setAttribute('aria-selected', String(active))
  })
  byId('quality-heading').textContent = scope === 'patch' ? 'Patch quality review' : 'Source image quality review'
  byId('quality-description').textContent = scope === 'patch'
    ? 'Review individual classification patches first. Patch decisions affect only that dataset item.'
    : 'Review full source images second. Source decisions apply to every annotation from that image.'
  byId('quality-filter').value = scope === 'patch' ? 'pending' : ''
  loadQualityReview()
}

document.querySelectorAll('[data-quality-scope]').forEach(button => button.addEventListener('click', () => setQualityScope(button.dataset.qualityScope)))

function qualityCandidates(decision) {
  return state.qualityItems.filter(item => item.quality.decision === decision &&
    !item.quality.flags.includes('analysis_failed') && item.savedDecision !== decision)
}

function updateQualityBulkButtons() {
  byId('quality-accept-all').textContent = `Include recommended (${qualityCandidates('accept').length})`
  byId('quality-reject-all').textContent = `Exclude recommended (${qualityCandidates('reject').length})`
}

async function applyQualityRecommendations(decision) {
  const candidates = qualityCandidates(decision)
  if (!candidates.length) {
    updateQualityProgress(0, 0, `No ${decision === 'accept' ? 'usable' : 'poor'} recommendations left to apply`)
    return
  }
  const verb = decision === 'accept' ? 'Including' : 'Excluding'
  let applied = 0
  const conflicts = []
  for (let start = 0; start < candidates.length; start += 500) {
    const chunk = candidates.slice(start, start + 500)
    updateQualityProgress(start, candidates.length, `${verb} ${candidates.length} recommended…`)
    const result = await request(`/api/curation/projects/${state.projectId}/quality/bulk`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        scope: state.qualityScope === 'patch' ? 'patch' : 'source',
        decisions: chunk.map(item => ({ item_id: item.reviewId, decision, score: item.quality.score, flags: item.quality.flags, expected_revision: item.revision ?? null })),
      }),
    })
    applied += result.applied
    conflicts.push(...result.conflicts)
  }
  updateQualityProgress(candidates.length, candidates.length, `${verb.replace('ing', 'ed')} ${applied}`)
  reportConflicts({ updated: applied, conflicts })
  await Promise.all([loadQualityReview(), loadStats()])
}

byId('quality-filter').addEventListener('change', () => loadQualityReview())
byId('quality-load-more').addEventListener('click', () => loadQualityReview(true).catch(error => showNotice(error.message)))
byId('quality-signal-filter').addEventListener('change', () => {
  // Scored on the server: filter every patch there. Otherwise only the loaded ones, here.
  if (state.qualityPrepared && state.qualityScope === 'patch') loadQualityReview().catch(error => showNotice(error.message))
  else renderQualityList()
})
byId('quality-sort').addEventListener('change', renderQualityList)
byId('quality-accept-all').addEventListener('click', () => applyQualityRecommendations('accept'))
byId('quality-reject-all').addEventListener('click', () => applyQualityRecommendations('reject'))

function selectedRunId() {
  const source = byId('matrix-source').value
  return source.startsWith('run:') ? source.slice(4) : null
}

function rememberEvaluation() {
  try {
    localStorage.setItem(`curation-evaluation:${state.projectId}`, JSON.stringify({
      source: byId('matrix-source').value, split: byId('evaluation-split').value,
    }))
  } catch (_) {}
}

async function loadEvaluationCatalog(preferred = null) {
  clearTimeout(evaluationState.timer)
  if (!state.projectId) return
  const projectId = state.projectId
  const [catalog, modelCatalog, versionCatalog] = await Promise.all([
    request(`/api/curation/projects/${projectId}/evaluations`), request('/api/curation/evaluation-models'),
    request(`/api/curation/projects/${projectId}/versions`),
  ])
  if (state.projectId !== projectId) return
  const sameProject = evaluationState.projectId === projectId
  const previous = sameProject ? byId('matrix-source').value : null
  let remembered = null
  try { remembered = JSON.parse(localStorage.getItem(`curation-evaluation:${projectId}`)) } catch (_) {}
  evaluationState.projectId = projectId
  evaluationState.runs = catalog.evaluations
  evaluationState.models = modelCatalog.models
  evaluationState.benchmarks = catalog.benchmarks
  evaluationState.workerAvailable = catalog.worker_available
  const completed = catalog.evaluations.filter(run => run.available)
  const benchmarks = catalog.benchmarks.filter(run => run.available)
  const select = byId('matrix-source')
  select.innerHTML = '<option value="">No completed evaluation</option>' +
    completed.map(run => `<option value="run:${run.id}">${escapeHtml(run.name)} · ${escapeHtml(timeAgo(run.completed_at))}</option>`).join('') +
    benchmarks.map(run => `<option value="t3_v0_test">External ${escapeHtml(run.name)}</option>`).join('') +
    '<option value="imported_csv">All project items / CSV suggestions</option>'
  const requested = preferred || evaluationState.preferred
  const wanted = requested && completed.some(run => `run:${run.id}` === requested) ? requested : previous || remembered?.source
  if (wanted === evaluationState.preferred) evaluationState.preferred = null
  const exists = [...select.options].some(option => option.value === wanted)
  select.value = exists ? wanted : completed.length ? `run:${completed[0].id}`
    : currentProject()?.source_config?.baseline_evaluation === 't3-v0-test' && benchmarks.length ? 't3_v0_test' : ''
  if (!sameProject && remembered?.split) byId('evaluation-split').value = remembered.split
  const versions = versionCatalog.versions.filter(version => version.status === 'published')
  evaluationState.publishedVersions = versions.length
  const versionSelect = byId('evaluation-version')
  const priorVersion = sameProject ? versionSelect.value : null
  versionSelect.innerHTML = versions.length ? versions.map(version => `<option value="${version.version}">v${version.version} · ${version.item_count.toLocaleString()} items</option>`).join('') : '<option value="">No published version</option>'
  if (priorVersion && versions.some(version => String(version.version) === priorVersion)) versionSelect.value = priorVersion
  if (state.pendingRun && [...byId('matrix-source').options].some(option => option.value === `run:${state.pendingRun}`)) {
    byId('matrix-source').value = `run:${state.pendingRun}`
  }
  state.pendingRun = null
  const modelSelect = byId('evaluation-model')
  const priorModel = modelSelect.value
  modelSelect.innerHTML = '<option value="">Choose classifier</option>' + modelCatalog.models.filter(model => model.status === 'ready').map(model => `<option value="${model.id}">${escapeHtml(model.name)} · ${model.sha256.slice(0, 8)}</option>`).join('')
  if (modelCatalog.models.some(model => model.id === priorModel && model.status === 'ready')) modelSelect.value = priorModel
  const reference = byId('evaluation-model-reference')
  const previousRef = reference.value
  reference.innerHTML = '<option value="">Upload weights</option>' + modelCatalog.configured.map(model => `<option value="${escapeHtml(model.ref)}">${escapeHtml(model.name)}</option>`).join('')
  if (modelCatalog.configured.some(model => model.ref === previousRef)) reference.value = previousRef
  byId('evaluation-worker-state').textContent = catalog.worker_available ? 'Worker online' : 'Worker unavailable'
  byId('start-evaluation').disabled = !canDo('project-curator') || !catalog.worker_available || !versions.length
  byId('evaluation-runs').innerHTML = catalog.evaluations.slice(0, 10).map(run => `<div><span><b>${escapeHtml(run.name)}</b> · ${escapeHtml(run.status)} · ${run.scored_count.toLocaleString()}/${run.expected_count.toLocaleString()}${run.has_missing ? ` · <a href="/api/curation/projects/${state.projectId}/evaluations/${run.id}/missing.csv">${run.unscored_count.toLocaleString()} missing patches</a>` : ''}${run.error_message ? ` · ${escapeHtml(run.error_message)}` : ''}</span>${['queued', 'staging', 'running'].includes(run.status) && canDo('project-curator') ? `<button class="btn btn-sm btn-outline-danger" type="button" data-cancel-evaluation="${run.id}" ${run.cancel_requested ? 'disabled' : ''}><i class="bi bi-stop-fill"></i> Cancel</button>` : ''}${run.status === 'completed' ? `<button class="btn btn-sm btn-outline-primary" type="button" data-open-evaluation="${run.id}"><i class="bi bi-grid"></i> Results</button>` : ''}</div>`).join('')
  byId('evaluation-models').innerHTML = modelCatalog.models.map(model => `<div><span><b>${escapeHtml(model.name)}</b> · ${escapeHtml(model.status)} · <code>${model.sha256.slice(0, 12)}</code>${model.error_message ? ` · ${escapeHtml(model.error_message)}` : ''}</span>${['staged', 'failed'].includes(model.status) && canDo('create') ? `<button type="button" class="btn btn-sm btn-outline-secondary" data-validate-evaluation-model="${model.id}" ${catalog.worker_available ? '' : 'disabled'}><i class="bi bi-check2-circle"></i> Validate</button>` : ''}</div>`).join('')
  document.querySelectorAll('[data-validate-evaluation-model]').forEach(button => button.addEventListener('click', async () => {
    await request(`/api/curation/evaluation-models/${button.dataset.validateEvaluationModel}/validate`, { method: 'POST' })
    await loadEvaluationCatalog()
  }))
  document.querySelectorAll('[data-cancel-evaluation]').forEach(button => button.addEventListener('click', async () => {
    await request(`/api/curation/projects/${projectId}/evaluations/${button.dataset.cancelEvaluation}/cancel`, { method: 'POST' })
    await loadEvaluationCatalog()
  }))
  document.querySelectorAll('[data-open-evaluation]').forEach(button => button.addEventListener('click', async () => {
    select.value = `run:${button.dataset.openEvaluation}`
    changeEvaluationSource()
  }))
  configureEvaluationSplit()
  if (catalog.evaluations.some(run => ['queued', 'staging', 'running'].includes(run.status)) || modelCatalog.models.some(model => model.status === 'validating')) {
    evaluationState.timer = setTimeout(async () => {
      try {
        await loadEvaluationCatalog()
        if (state.view === 'matrix' && byId('matrix-source').value !== previous) await loadConfusionMatrix()
        else if (state.view === 'matrix' && !byId('matrix-source').value) renderMatrixEmptyState()
      } catch (error) { showNotice(error.message) }
    }, 2000)
  }
}

function configureEvaluationSplit() {
  const run = evaluationState.runs.find(run => run.id === selectedRunId())
  const pinned = byId('matrix-source').value === 't3_v0_test'
  const splits = run?.splits || (pinned ? evaluationState.benchmarks[0]?.splits || ['test'] : [])
  const select = byId('evaluation-split')
  select.disabled = !splits.length
  ;[...select.options].forEach(option => { option.disabled = option.value === 'all' ? !splits.length : !splits.includes(option.value) })
  if (![...select.options].some(option => option.value === select.value && !option.disabled)) select.value = splits.includes('test') ? 'test' : splits[0] || 'test'
  byId('official-matrix-link').hidden = !pinned
}

function changeEvaluationSource() {
  queueMicrotask(renderFilterChipBar)
  state.matrixFilter = null
  state.matrixLinkedItemIds = []
  state.evaluationItems = []
  evaluationState.editContext = null
  byId('evaluation-drilldown').classList.add('d-none')
  renderMatrixDrilldown()
  configureEvaluationSplit()
  rememberEvaluation()
  loadConfusionMatrix()
}

async function registerEvaluationModel(event) {
  event.preventDefault()
  const button = byId('register-evaluation-model')
  button.disabled = true
  try {
    const file = byId('evaluation-model-file').files[0]
    const name = byId('evaluation-model-name').value.trim()
    const trusted = byId('evaluation-model-trusted').checked
    let model
    if (file) {
      const body = new FormData()
      body.set('file', file)
      body.set('name', name)
      body.set('trusted', String(trusted))
      model = await request('/api/curation/evaluation-models/upload', { method: 'POST', body })
    } else {
      model = await request('/api/curation/evaluation-models', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ref: byId('evaluation-model-reference').value, name, trusted }) })
    }
    event.target.reset()
    await loadEvaluationCatalog()
    if (byId('evaluation-worker-state').textContent === 'Worker online') {
      await request(`/api/curation/evaluation-models/${model.id}/validate`, { method: 'POST' })
      await loadEvaluationCatalog()
    }
  } finally { button.disabled = !canDo('create') }
}

async function startEvaluation(event) {
  event.preventDefault()
  const button = byId('start-evaluation')
  button.disabled = true
  try {
    const splits = [...document.querySelectorAll('[name="evaluation-splits"]:checked')].map(input => input.value)
    if (!splits.length) throw new Error('Choose at least one split')
    const run = await request(`/api/curation/projects/${state.projectId}/evaluations`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version: Number(byId('evaluation-version').value), classifier_id: byId('evaluation-model').value,
        splits, batch_size: Number(byId('evaluation-batch').value), device: byId('evaluation-device').value }) })
      evaluationState.preferred = `run:${run.id}`
    await loadEvaluationCatalog()
  } finally { button.disabled = !canDo('project-curator') || byId('evaluation-worker-state').textContent !== 'Worker online' }
}

async function loadEvaluationDraftState() {
  const projectId = state.projectId
  const preview = await request(`/api/curation/projects/${projectId}/versions/preview`)
  if (projectId !== state.projectId) return
  const changes = preview.changes
  byId('evaluation-draft-state').textContent = preview.base_version
    ? `Draft vs v${preview.base_version}: ${changes.in.toLocaleString()} added · ${changes.out.toLocaleString()} excluded · ${changes.relabelled.toLocaleString()} relabelled`
    : 'No published dataset version'
}

// With no completed evaluation, say where one stands rather than showing an empty table: a run in
// progress (on a new install, the starter baseline), one that did not finish, or what is still missing.
function renderMatrixEmptyState() {
  const box = byId('matrix-loading')
  const runs = evaluationState.runs || []
  const models = evaluationState.models || []
  const active = runs.find(run => ['queued', 'staging', 'running'].includes(run.status))
  byId('matrix-subtitle').textContent = active ? 'Evaluation in progress' : 'No completed model evaluation'
  if (active) {
    // Two passes: the app fetches every patch for the worker (staging), then the worker scores them.
    const staged = active.provenance?.staged_count || 0
    const done = active.status === 'running' ? active.scored_count : active.status === 'staging' ? staged : 0
    const pct = active.expected_count ? Math.floor(done / active.expected_count * 100) : 0
    const stage = active.status === 'running'
      ? `Scoring: ${active.scored_count.toLocaleString()} of ${active.expected_count.toLocaleString()} patches`
      : active.status === 'staging' ? `Fetching patches for the worker: ${staged.toLocaleString()} of ${active.expected_count.toLocaleString()}`
      : 'Waiting for the evaluation worker'
    box.innerHTML = `<div class="matrix-empty"><strong>Evaluating ${escapeHtml(active.name)}</strong>
      <p>${stage}. The matrix opens here when it finishes.</p>
      <div class="matrix-empty-progress" role="progressbar" aria-label="Evaluation progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i style="width:${pct}%"></i></div></div>`
    return
  }
  const last = runs[0]
  const unfinished = last && ['failed', 'interrupted', 'cancelled', 'canceled'].includes(last.status) ? last : null
  const config = currentProject()?.source_config || {}
  const starterPending = config.starter === 'noaa-t3-hub' && !config.baseline_run
  const steps = [
    [evaluationState.publishedVersions > 0, 'A published dataset version', 'publish one from Versions'],
    [models.some(model => model.status === 'ready'), 'A validated classifier',
      models.some(model => model.status === 'validating') ? 'validating now' : 'register or validate one below'],
    [evaluationState.workerAvailable, 'The evaluation worker online', 'start the oceanframe-evaluation service'],
  ]
  box.innerHTML = `<div class="matrix-empty"><strong>${unfinished ? 'The last evaluation did not finish' : 'No evaluation yet'}</strong>
    <p>${unfinished ? `${escapeHtml(unfinished.error_message || unfinished.status)}. ` : ''}The matrix compares a classifier's predictions with this project's labels. Scoring one needs:</p>
    <ul class="matrix-empty-steps">${steps.map(([done, label, hint]) => `<li class="${done ? 'done' : ''}"><i class="bi ${done ? 'bi-check-circle-fill' : 'bi-circle'}"></i> ${label}${done ? '' : ` <small>(${hint})</small>`}</li>`).join('')}</ul>
    <p>${starterPending ? 'For this starter project that happens by itself: when the import finishes, v1 is published and the T3 classifier is scored on train, val and test.'
      : 'Then start an evaluation under <b>Models and evaluation runs</b> below.'}</p></div>`
  byId('evaluation-tools').open = !starterPending
}

async function loadConfusionMatrix() {
  if (!state.projectId) return
  const source = byId('matrix-source').value
  const projectId = state.projectId
  state.matrix = null
  byId('matrix-kpis').innerHTML = ''
  byId('matrix-provenance-text').textContent = ''
  byId('evaluation-draft-state').textContent = ''
  byId('matrix-scroll').classList.add('d-none')
  byId('matrix-loading').classList.remove('d-none')
  if (!source) {
    renderMatrixEmptyState()
    return
  }
  const params = currentFilters()
  params.delete('limit')
  params.delete('source_labels')
  params.delete('model_labels')
  const ordinaryCodes = new Set(state.selectedCodes)
  state.selectedTaxonomy.forEach(node => node.codes.forEach(code => ordinaryCodes.add(code)))
  if (ordinaryCodes.size) params.set('labels', [...ordinaryCodes].join(','))
  else params.delete('labels')
  params.set('actual_axis', byId('matrix-actual-axis').value)
  params.set('normalization', byId('matrix-normalization').value)
  params.set('matrix_source', selectedRunId() ? 'evaluation' : source)
  if (selectedRunId()) params.set('evaluation_id', selectedRunId())
  params.set('split', byId('evaluation-split').value)
  const evaluation = source !== 'imported_csv'
  byId('matrix-subtitle').textContent = source === 'imported_csv' ? 'All project items / imported suggestions' : source === 't3_v0_test' ? 'External T3 benchmark / original dataset' : 'Published dataset / saved model predictions'
  byId('matrix-actual-axis').disabled = evaluation
  if (evaluation) byId('matrix-actual-axis').value = 'source'
  byId('matrix-loading').textContent = 'Loading matrix...'
  byId('matrix-loading').classList.remove('d-none')
  byId('matrix-scroll').classList.add('d-none')
  try {
    const matrix = await request(`/api/curation/projects/${projectId}/confusion-matrix?${params}`)
    if (projectId !== state.projectId || source !== byId('matrix-source').value ||
      params.get('split') !== byId('evaluation-split').value || params.get('normalization') !== byId('matrix-normalization').value) return
    state.matrix = matrix
    renderConfusionMatrix()
    await loadEvaluationDraftState()
  } catch (error) {
    byId('matrix-loading').textContent = error.message
  }
}

function renderConfusionMatrix() {
  const matrix = state.matrix
  if (!matrix) return
  const coverage = matrix.scored_count / Math.max(1, matrix.scored_count + matrix.unscored_count)
  const kpis = [
    ['Scored', matrix.scored_count.toLocaleString()], ['Coverage', `${(coverage * 100).toFixed(1)}%`],
    ['Accuracy', `${(matrix.accuracy * 100).toFixed(1)}%`], ['Correct', matrix.correct_count.toLocaleString()],
    ['Actual classes', matrix.rows.length], ['Predicted classes', matrix.columns.length],
  ]
  if (matrix.metrics?.top5_accuracy != null) kpis.splice(3, 0, ['Top-5', `${(matrix.metrics.top5_accuracy * 100).toFixed(1)}%`])
  if (matrix.metrics?.macro_f1 != null) kpis.push(['Macro F1', `${(matrix.metrics.macro_f1 * 100).toFixed(1)}%`])
  byId('matrix-kpis').innerHTML = kpis.map(([label, value]) => `<div><strong>${value}</strong><span>${label}</span></div>`).join('')
  byId('matrix-provenance-text').textContent = matrix.matrix_source === 'evaluation'
    ? `${matrix.model.name} · v${matrix.model.dataset_version} · ${matrix.split === 'all' ? 'All evaluated splits (mixed)' : matrix.split} · model ${matrix.model.model_sha256.slice(0, 12)} · inputs ${matrix.model.manifest_sha256?.slice(0, 12)}${matrix.model.test_informed ? ' · Test-informed dataset' : ''}${matrix.model.cross_split_source_images ? ` · ${matrix.model.cross_split_source_images} source images cross splits` : ''}`
    : matrix.model.provenance_complete
      ? `${matrix.model.name} · External ${matrix.model.dataset_split} benchmark · model ${matrix.model.model_revision?.slice(0, 8)} · dataset ${matrix.model.dataset_revision?.slice(0, 8)}`
      : `${matrix.model.name} · Imported suggestions / incomplete model provenance`
  if (!matrix.cells.length) {
    // Say why there is nothing to draw instead of showing an empty frame.
    byId('matrix-table').innerHTML = ''
    byId('matrix-scroll').classList.add('d-none')
    byId('matrix-loading').classList.remove('d-none')
    byId('matrix-loading').textContent = matrix.unscored_count
      ? `None of the ${matrix.unscored_count.toLocaleString()} items in this selection has a classifier suggestion to compare against.`
      : 'No items match the current filters.'
    return
  }
  const search = byId('matrix-search').value.trim().toLowerCase()
  const rows = matrix.rows.filter(row => !search || row.label.toLowerCase().includes(search))
  const columns = matrix.columns.filter(column => !search || column.label.toLowerCase().includes(search))
  const order = byId('matrix-class-order').value
  if (order !== 'model') {
    rows.sort((left, right) => order === 'recall'
      ? Number(!left.total) - Number(!right.total) || left.metric - right.metric || right.total - left.total
      : (right.total - right.correct) - (left.total - left.correct) || right.total - left.total)
    const indices = new Map(rows.map((row, index) => [row.label, index]))
    columns.sort((left, right) => (indices.get(left.label) ?? rows.length) - (indices.get(right.label) ?? rows.length))
  }
  const cellMap = new Map(matrix.cells.map(cell => [`${cell.actual}\u0000${cell.predicted}`, cell]))
  const maxCount = Math.max(1, ...matrix.cells.map(cell => cell.count))
  const header = `<thead><tr><th class="matrix-corner">Actual \\ Predicted</th>${columns.map(column => `<th><button type="button" data-matrix-predicted="${escapeHtml(column.label)}" title="Filter predicted ${escapeHtml(column.label)}">${escapeHtml(column.label)}<small>P ${(column.metric * 100).toFixed(0)}%</small></button></th>`).join('')}<th>Total</th></tr></thead>`
  const body = rows.map(row => `<tr><th><button type="button" data-matrix-actual="${escapeHtml(row.label)}" title="Filter actual ${escapeHtml(row.label)}">${escapeHtml(row.label)}<small>R ${(row.metric * 100).toFixed(0)}%</small></button></th>${columns.map(column => {
    const cell = cellMap.get(`${row.label}\u0000${column.label}`)
    if (!cell) return '<td class="matrix-zero"></td>'
    const intensity = matrix.normalization === 'count' ? Math.log1p(cell.count) / Math.log1p(maxCount) : cell.value
    const percentage = matrix.normalization === 'count' ? '' : ` · ${(cell.value * 100).toFixed(1)}%`
    return `<td><button type="button" class="matrix-cell ${row.label === column.label ? 'diagonal' : ''}" data-matrix-cell="1" data-actual="${escapeHtml(row.label)}" data-predicted="${escapeHtml(column.label)}" style="--intensity:${Math.max(.08, intensity)}" title="Actual ${escapeHtml(row.label)}, predicted ${escapeHtml(column.label)}: ${cell.count.toLocaleString()}${percentage}">${cell.count.toLocaleString()}</button></td>`
  }).join('')}<td class="matrix-total">${row.total.toLocaleString()}</td></tr>`).join('')
  byId('matrix-table').innerHTML = header + `<tbody>${body}</tbody>`
  applyMatrixZoom()
  document.querySelectorAll('[data-matrix-cell]').forEach(button => button.addEventListener('click', () => applyMatrixDrilldown(button.dataset.actual, button.dataset.predicted)))
  document.querySelectorAll('[data-matrix-actual]').forEach(button => button.addEventListener('click', () => applyMatrixDrilldown(button.dataset.matrixActual, null)))
  document.querySelectorAll('[data-matrix-predicted]').forEach(button => button.addEventListener('click', () => applyMatrixDrilldown(null, button.dataset.matrixPredicted)))
  byId('matrix-loading').classList.add('d-none')
  byId('matrix-scroll').classList.remove('d-none')
}

function applyMatrixZoom() {
  const zoom = Math.max(0.4, Math.min(2, state.matrixZoom))
  state.matrixZoom = zoom
  byId('matrix-table').style.setProperty('--matrix-scale', zoom)
  byId('matrix-zoom-level').textContent = `${Math.round(zoom * 100)}%`
}

function zoomMatrix(factor) {
  state.matrixZoom *= factor
  applyMatrixZoom()
}

function fitMatrixToWidth() {
  if (!state.matrix) return
  const search = byId('matrix-search').value.trim().toLowerCase()
  const visibleColumns = state.matrix.columns.filter(column => !search || column.label.toLowerCase().includes(search)).length
  const baseWidth = 112 + visibleColumns * 35 + 72
  const available = Math.max(320, byId('matrix-scroll').clientWidth - 4)
  state.matrixZoom = available / Math.max(1, baseWidth)
  applyMatrixZoom()
}

function resetMatrixZoom() {
  state.matrixZoom = 1
  applyMatrixZoom()
}

function applyMatrixDrilldown(actual, predicted) {
  resetSourceWorkspace()
  if (state.matrix?.drilldown_kind === 'evaluation_predictions') {
    state.matrixFilter = { actualAxis: 'source', actual, predicted, evaluation: true, runId: selectedRunId() }
    state.matrixLinkedItemIds = []
    state.evaluationSplit = byId('evaluation-split').value
    byId('evaluation-evidence-kind').value = 'predictions'
    state.selectedMatrixClasses = new Set([actual, predicted].filter(Boolean))
    state.selectedMatrixItemIds.clear()
    // The crops render under a tall table; bring them into view once they arrive.
    state.scrollToEvidence = true
    renderMatrixDrilldown()
    loadMatrixAssets(false)
    return
  }
  let modelCodes = predicted ? [predicted] : []
  if (predicted && (currentProject()?.dataset_mode || 'T3') === 'T1') {
    const node = state.taxonomy.find(candidate => candidate.tier === 'TIER_1' && candidate.code === predicted)
    if (node) modelCodes = node.codes
  }
  state.matrixFilter = {
    actualAxis: byId('matrix-actual-axis').value,
    actual,
    predicted,
    modelCodes,
  }
  renderMatrixDrilldown()
  state.backTo = makeBackTarget('matrix')
  setView('source')
}

function renderMatrixDrilldown() {
  const filter = state.matrixFilter
  byId('matrix-drilldown').classList.toggle('d-none', !filter)
  if (!filter) return
  const parts = []
  if (filter.label) parts.push(filter.label)
  if (filter.actual) parts.push(`Actual (${filter.actualAxis}): ${filter.actual}`)
  if (filter.predicted) parts.push(`Predicted: ${filter.predicted}`)
  if (filter.runId) parts.push(`${state.evaluationSplit === 'val' ? 'Validation' : state.evaluationSplit} split`)
  byId('matrix-drilldown-text').textContent = `Matrix drilldown · ${parts.join(' · ')}`
  byId('matrix-to-quality').classList.toggle('d-none', state.view === 'quality')
  byId('matrix-cleanup').classList.toggle('d-none', !cleanupCell())
}

// ── Clean-up from the matrix (curation/cleanup.py): one action for a whole off-diagonal cell ─────────────
let cleanupPreview = null

function cleanupCell() {
  const filter = state.matrixFilter
  if (!filter?.runId || !filter.actual || !filter.predicted || filter.actual === filter.predicted) return null
  return { runId: filter.runId, actual: filter.actual, predicted: filter.predicted }
}

function cleanupSplits() {
  return [...document.querySelectorAll('[data-cleanup-split]')].filter(box => box.checked).map(box => box.dataset.cleanupSplit)
}

async function loadCleanupPreview() {
  const cell = cleanupCell()
  const splits = cleanupSplits()
  cleanupPreview = null
  document.querySelectorAll('[data-cleanup-action]').forEach(button => { button.disabled = true })
  if (!cell || !splits.length) { byId('cleanup-preview').textContent = 'Choose at least one split.'; return }
  const base = `/api/curation/projects/${state.projectId}/evaluations/${cell.runId}/cleanup`
  const query = new URLSearchParams({ splits: splits.join(',') })
  const [preview, tally] = await Promise.all([
    request(`${base}?${new URLSearchParams({ actual: cell.actual, predicted: cell.predicted, splits: splits.join(',') })}`),
    request(`${base}/tally?${query}`),
  ])
  cleanupPreview = preview
  const perSplit = Object.entries(preview.by_split).map(([split, entry]) => `${split} ${fmt(entry.patches)}`).join(' · ')
  byId('cleanup-preview').textContent = preview.patches
    ? `${fmt(preview.patches)} patches (${perSplit}) · cleaned ${fmt(preview.cleaned)} of ${fmt(preview.patches)}.`
    : 'No patches of this cell in these splits.'
  const own = tally.classes.find(entry => entry.actual === cell.actual)
  byId('cleanup-class-tally').textContent = own
    ? `${cell.actual} overall: cleaned ${fmt(own.cleaned)} of ${fmt(own.patches)} confused patches; whole matrix: ${fmt(tally.cleaned)} of ${fmt(tally.patches)}.`
    : ''
  document.querySelectorAll('[data-cleanup-action]').forEach(button => { button.disabled = !preview.patches || !canDo('project-curator') })
}

async function openCleanup() {
  const cell = cleanupCell()
  if (!cell) return
  byId('cleanup-subtitle').textContent = `${cell.actual} called ${cell.predicted}`
  byId('cleanup-relabel').textContent = `Relabel to ${cell.predicted}`
  byId('cleanup-message').textContent = ''
  byId('cleanup-dialog').showModal()
  await loadCleanupPreview().catch(error => { byId('cleanup-preview').textContent = error.message })
}

const CLEANUP_VERBS = { relabel: 'Relabel', exclude: 'Exclude', flag: 'Flag for a re-split' }

async function applyCleanup(action) {
  const cell = cleanupCell()
  const preview = cleanupPreview
  if (!cell || !preview) return
  const todo = preview.patches - preview.cleaned
  const testInformed = preview.splits.includes('test')
  const ok = await window.OF.ui.confirm({
    title: `${CLEANUP_VERBS[action]} ${fmt(todo)} patches?`,
    message: `${cell.actual} patches the model called ${cell.predicted}, in ${preview.splits.join(', ')}.` +
      (action === 'relabel' ? ` Their label becomes ${cell.predicted}.` : '') +
      (testInformed ? ' Test patches are included: this is a test-informed change and only affects a future dataset version.' : ''),
    confirmLabel: CLEANUP_VERBS[action], danger: action === 'exclude' || testInformed,
  })
  if (!ok) return
  const message = byId('cleanup-message')
  try {
    const result = await request(`/api/curation/projects/${state.projectId}/evaluations/${cell.runId}/cleanup`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ actual: cell.actual, predicted: cell.predicted, action, splits: preview.splits,
        watermark: preview.watermark, confirm_test: testInformed }),
    })
    const notes = [`Done: ${fmt(result.applied)} changed`]
    if (result.already) notes.push(`${fmt(result.already)} already were`)
    if (result.conflict_count) notes.push(`${fmt(result.conflict_count)} changed by someone since the preview, left alone`)
    message.textContent = `${notes.join('; ')}.`
    message.className = 'small mt-2 mb-0 text-success'
    await loadCleanupPreview()
    if (state.matrixFilter?.evaluation && typeof loadMatrixAssets === 'function') loadMatrixAssets(false).catch(() => {})
  } catch (error) {
    message.textContent = error.message
    message.className = 'small mt-2 mb-0 text-danger'
  }
}

// The cell's patches in Quality review, every decision shown; clearing the matrix filter there
// leaves the other filters, so the same view then shows every patch.
function openMatrixInQuality() {
  const from = state.view
  if (state.qualityScope !== 'patch') {
    state.qualityScope = 'patch'
    document.querySelectorAll('[data-quality-scope]').forEach(button => {
      button.classList.toggle('active', button.dataset.qualityScope === 'patch')
      button.setAttribute('aria-selected', String(button.dataset.qualityScope === 'patch'))
    })
  }
  byId('quality-filter').value = ''
  return setView('quality', from === 'quality' ? null : makeBackTarget(from))
}

async function loadMatrixAssets(append) {
  const filter = state.matrixFilter
  if (!filter?.evaluation || !state.matrix?.evaluation_id) return
  const requestId = ++evaluationState.assetRequest
  const projectId = state.projectId
  const params = new URLSearchParams({ limit: '60', split: state.evaluationSplit })
  params.set('evaluation_id', state.matrix.evaluation_id)
  const inventory = byId('evaluation-evidence-kind').value === 'inventory'
  params.set('evidence_kind', inventory ? 'inventory' : 'predictions')
  if (byId('filter-show-removed').checked) params.set('removed', 'any')  // hidden by default, like everywhere else
  if (!inventory) {
    params.set('sort_by', state.evaluationSort)
    if (filter.actual) params.set('actual_label', filter.actual)
    if (filter.predicted) params.set('predicted_label', filter.predicted)
  } else {
    if (filter.actual && state.selectedMatrixClasses.has(filter.actual)) params.set('actual_label', filter.actual)
    if (filter.predicted && state.selectedMatrixClasses.has(filter.predicted)) params.set('predicted_label', filter.predicted)
  }
  if (append && state.evaluationCursor) params.set('cursor', state.evaluationCursor)
  const page = await request(`/api/curation/projects/${state.projectId}/matrix-assets?${params}`)
  if (requestId !== evaluationState.assetRequest || projectId !== state.projectId ||
      params.get('evaluation_id') !== state.matrix?.evaluation_id || params.get('split') !== state.evaluationSplit ||
      params.get('evidence_kind') !== byId('evaluation-evidence-kind').value) return
  state.evaluationItems = append ? [...state.evaluationItems, ...page.items] : page.items
  page.items.forEach(item => {
    if (item.curation_link.item_id && item.gcs_uri) state.resolvedCropUris.set(item.curation_link.item_id, item.gcs_uri)
  })
  state.matrixLinkedItemIds = [...new Set(state.evaluationItems.map(item => item.curation_link.item_id).filter(Boolean))]
  if (!append) state.selectedMatrixItemIds.clear()
  state.evaluationCursor = page.next_cursor
  renderEvaluationGrid()
  if (state.view === 'source') loadQueue(false)
}

function renderEvaluationGrid() {
  applyGridSizing()
  const filter = state.matrixFilter
  const savedRun = !!selectedRunId()
  const inventory = byId('evaluation-evidence-kind').value === 'inventory'
  const mutableSplit = state.evaluationItems.some(item => item.mutable)
  byId('evaluation-drilldown').classList.remove('d-none')
  if (state.scrollToEvidence) {
    state.scrollToEvidence = false
    const smooth = !window.matchMedia('(prefers-reduced-motion: reduce)').matches
    requestAnimationFrame(() => byId('evaluation-drilldown').scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'start' }))
  }
  const splitLabel = state.evaluationSplit === 'val' ? 'Validation' : state.evaluationSplit[0].toUpperCase() + state.evaluationSplit.slice(1)
  byId('evaluation-drilldown-title').textContent = `${splitLabel} · ` + [filter.actual ? `Actual ${filter.actual}` : null, filter.predicted ? `${state.evaluationSplit === 'test' ? 'Predicted' : 'Compared'} ${filter.predicted}` : null].filter(Boolean).join(' · ')
  byId('evaluation-drilldown-count').textContent = `${state.evaluationItems.length} crops loaded`
  byId('matrix-split-note').textContent = !inventory ? 'Saved evaluation / draft corrections only' : mutableSplit
    ? 'Training/validation cards are dataset class members. Editable actions require a unique curation match.'
    : 'Pinned test evidence is immutable. Full-image navigation is available only for unique curation matches.'
  byId('matrix-asset-bulk').classList.toggle('d-none', !mutableSplit)
  byId('matrix-evidence-sort').classList.toggle('d-none', inventory)
  byId('matrix-evidence-sort').value = state.evaluationSort
  byId('matrix-class-mode').classList.toggle('d-none', !inventory || state.evaluationSplit === 'test' || state.evaluationSplit === 'all')
  if (inventory) byId('matrix-class-mode').value = state.matrixClassModes[state.evaluationSplit] || 'both'
  renderMatrixClassToggles()
  document.querySelectorAll('[data-matrix-split]').forEach(button => {
    button.classList.toggle('active', button.dataset.matrixSplit === state.evaluationSplit)
    button.disabled = !inventory && button.dataset.matrixSplit !== 'all' && !state.matrix.available_splits.includes(button.dataset.matrixSplit)
    button.hidden = inventory && !savedRun && ['test', 'all'].includes(button.dataset.matrixSplit)
  })
  byId('evaluation-grid').innerHTML = state.evaluationItems.map(item => `<article class="curation-crop-card evaluation-card ${item.curation_link.item_id && state.selectedMatrixItemIds.has(item.curation_link.item_id) ? 'selected' : ''} ${item.predicted_label && item.actual_label === item.predicted_label ? 'correct' : item.predicted_label ? 'incorrect' : ''}" ${item.mutable && item.curation_link.item_id ? `data-grid-select-id="${item.curation_link.item_id}"` : ''} style="--label-color:${labelColor(item.actual_label)}" tabindex="0">
    ${item.mutable ? `<input class="crop-select" type="checkbox" data-select-matrix-item="${item.curation_link.item_id}" ${state.selectedMatrixItemIds.has(item.curation_link.item_id) ? 'checked' : ''} aria-label="Select crop">` : ''}
    <span class="crop-media"><img src="${item.preview_url || proxyUrl(item.gcs_uri)}" alt="Crop actual ${escapeHtml(item.actual_label)} predicted ${escapeHtml(item.predicted_label)}" loading="lazy"><i class="bi bi-image"></i></span>
    <span class="crop-card-meta"><strong><i></i>${escapeHtml(item.actual_label)}${item.predicted_label ? ` → ${escapeHtml(item.predicted_label)}` : ''}</strong><small>${item.top1_confidence == null ? '' : `${(item.top1_confidence * 100).toFixed(1)}% · `}${escapeHtml(item.file_name)}</small><small>${escapeHtml(item.curation_link.match_status)}${item.curation_link.current_label ? ` · current ${escapeHtml(item.curation_link.current_label)}` : ''}</small></span>
    ${item.curation_link.item_id ? `<button type="button" class="open-source-button" data-open-matrix-item="${item.curation_link.item_id}"><i class="bi bi-arrows-angle-expand"></i> Full image</button>`: ''}
  </article>`).join('')
  document.querySelectorAll('[data-select-matrix-item]').forEach(checkbox => checkbox.addEventListener('change', () => {
    const itemId = Number(checkbox.dataset.selectMatrixItem)
    if (checkbox.checked) state.selectedMatrixItemIds.add(itemId)
    else state.selectedMatrixItemIds.delete(itemId)
    checkbox.closest('.curation-crop-card').classList.toggle('selected', checkbox.checked)
    updateMatrixBulkControls()
  }))
  document.querySelectorAll('[data-open-matrix-item]').forEach(button => button.addEventListener('click', async () => {
    const itemId = Number(button.dataset.openMatrixItem)
    const evidence = state.evaluationItems.find(item => item.curation_link.item_id === itemId)
    evaluationState.editContext = evidence && (selectedRunId() || evidence.prediction_id) ? { runId: state.matrix.evaluation_id, evidence } : null
    await openSource(itemId, 'matrix')
  }))
  byId('load-more-evaluation').classList.toggle('d-none', !state.evaluationCursor)
  setupGridKeyboard('evaluation-grid', state.selectedMatrixItemIds, updateMatrixBulkControls)
  setupGridLasso('evaluation-grid', 'matrix', state.selectedMatrixItemIds, updateMatrixBulkControls)
  updateMatrixBulkControls()
}

function renderMatrixClassToggles() {
  const target = byId('matrix-class-toggles')
  const filter = state.matrixFilter
  const mutableSplit = byId('evaluation-evidence-kind').value === 'inventory' && !['test', 'all'].includes(state.evaluationSplit)
  target.classList.toggle('d-none', !mutableSplit)
  if (!mutableSplit || !filter) { target.innerHTML = ''; return }
  const labels = [...new Set([filter.actual, filter.predicted].filter(Boolean))]
  target.innerHTML = labels.map(label => `<label style="--label-color:${labelColor(label)}"><input type="checkbox" value="${escapeHtml(label)}" ${state.selectedMatrixClasses.has(label) ? 'checked' : ''}><i></i>${escapeHtml(label)}</label>`).join('')
  target.querySelectorAll('input').forEach(input => input.addEventListener('change', () => {
    if (input.checked) state.selectedMatrixClasses.add(input.value)
    else if (state.selectedMatrixClasses.size > 1) state.selectedMatrixClasses.delete(input.value)
    else input.checked = true
    state.evaluationCursor = null
    state.selectedMatrixItemIds.clear()
    loadMatrixAssets(false)
  }))
}

function applyMatrixClassMode() {
  if (state.evaluationSplit === 'test' || !state.matrixFilter) return
  const mode = byId('matrix-class-mode').value
  state.matrixClassModes[state.evaluationSplit] = mode
  if (mode === 'actual') state.selectedMatrixClasses = new Set([state.matrixFilter.actual].filter(Boolean))
  else if (mode === 'predicted') state.selectedMatrixClasses = new Set([state.matrixFilter.predicted].filter(Boolean))
  else state.selectedMatrixClasses = new Set([state.matrixFilter.actual, state.matrixFilter.predicted].filter(Boolean))
  state.evaluationCursor = null
  state.selectedMatrixItemIds.clear()
  loadMatrixAssets(false)
}

function updateMatrixBulkControls() {
  byId('matrix-selection-count').textContent = `${state.selectedMatrixItemIds.size} selected`
  byId('matrix-selection-bar').classList.toggle('d-none', !state.selectedMatrixItemIds.size || !state.evaluationItems.some(item => item.mutable))
  const action = byId('matrix-bulk-action').value
  byId('matrix-bulk-label').classList.toggle('d-none', action !== 'relabel')
  byId('apply-matrix-bulk').disabled = !canDo('project-curator') || !state.selectedMatrixItemIds.size || !action || (action === 'relabel' && !validRelabelCode(byId('matrix-bulk-label').value))
}

async function applyMatrixBulkAction() {
  const action = byId('matrix-bulk-action').value
  if (!action || !state.selectedMatrixItemIds.size) return
  const revisions = new Map(state.evaluationItems.map(item => [item.curation_link.item_id, item.curation_link.revision]))
  const payload = {
    items: [...state.selectedMatrixItemIds].map(id => ({ id, expected_revision: revisions.get(id) ?? null })),
    reason: `Matrix ${state.evaluationSplit} bulk action`,
  }
  if (action === 'include') payload.included = true
  else if (action === 'exclude') payload.included = false
  else if (action === 'relabel') payload.current_label = validRelabelCode(byId('matrix-bulk-label').value)
  else payload.review_state = action
  if (selectedRunId() || state.evaluationItems.some(item => item.prediction_id)) {
    const chosen = state.evaluationItems.filter(item => state.selectedMatrixItemIds.has(item.curation_link.item_id))
    const testInformed = chosen.some(item => item.split === 'test')
    if (testInformed && !(await window.OF.ui.confirm({
      title: 'Apply test-informed corrections?',
      message: 'They go to the draft for a future version. The original evaluation will remain unchanged.',
      confirmLabel: 'Apply corrections',
    }))) return
    let conflicts = 0
    for (const evidence of chosen) {
      try {
        const path = evidence.prediction_id ? `predictions/${evidence.prediction_id}` : `inputs/${evidence.input_item_id}`
        await request(`/api/curation/projects/${state.projectId}/evaluations/${state.matrix.evaluation_id}/${path}/item`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...payload, items: undefined, expected_revision: evidence.curation_link.revision, confirm_test: testInformed, split: evidence.split }),
        })
      } catch (error) { if (error.status === 409) conflicts += 1; else throw error }
    }
    if (conflicts) showNotice(`${conflicts} corrections conflicted; reload and review the current labels.`)
    state.selectedMatrixItemIds.clear()
    await Promise.all([loadMatrixAssets(false), loadStats()])
    await loadEvaluationDraftState()
    return
  }
  const result = await request(`/api/curation/projects/${state.projectId}/items/bulk`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
  reportConflicts(result)
  state.selectedMatrixItemIds.clear()
  await Promise.all([loadMatrixAssets(false), loadStats()])
}

async function clearMatrixDrilldown() {
  if (state.view === 'source' && typeof confirmDiscardPending === 'function' && !(await confirmDiscardPending())) return
  state.matrixFilter = null
  state.matrixLinkedItemIds = []
  state.selectedMatrixItemIds.clear()
  state.selectedMatrixClasses.clear()
  renderMatrixDrilldown()
  byId('evaluation-drilldown').classList.add('d-none')
  if (state.view === 'source') resetSourceWorkspace()
  else applyActiveFilters()
}

// ── Split breakdowns ─────────────────────────────────────────────────────────
// One table for the live draft (Dataset status) and for each published version.
const SPLIT_COLORS = { train: 'var(--accent)', val: 'var(--warn)', test: 'var(--ok)', unassigned: 'var(--surface-3)' }
const splitsState = { data: null, sortKey: 'total' }

function renderBreakdownTable(container, data, { rareOnly = false, sortKey = 'total' } = {}) {
  const pct = (part, whole) => whole ? `${Math.round(part / whole * 100)}%` : '—'
  const rows = data.rows.filter(row => !rareOnly || row.rare)
    .sort((a, b) => sortKey === 'key' ? a.key.localeCompare(b.key) : (sortKey === 'total' ? b.total - a.total : b.counts[sortKey] - a.counts[sortKey]))
  const header = ['key', ...data.splits, 'total'].map(key => `<th class="${key === 'key' ? '' : 'num'}" data-split-sort="${key}" role="button">${key === 'key' ? escapeHtml(data.by.replace('_', ' ')) : key}${sortKey === key ? ' ▾' : ''}</th>`).join('')
  container.innerHTML = `<p class="small text-muted mb-1">${fmt(data.total)} items · train ${pct(data.totals.train, data.total)} · val ${pct(data.totals.val, data.total)} · test ${pct(data.totals.test, data.total)}${data.totals.unassigned ? ` · ${fmt(data.totals.unassigned)} get a split on publish` : ''} · rare = fewer than ${data.rare_min} in val or test</p>
    <div class="table-responsive"><table class="table table-sm align-middle mb-0"><thead><tr>${header}<th>Mix</th></tr></thead><tbody>
    ${rows.length ? rows.map(row => `<tr class="${row.rare ? 'split-rare' : ''}">
      <td>${data.by === 'label' ? `<i class="legend-dot" style="background:${labelColor(row.key)}"></i> ` : ''}<strong>${escapeHtml(row.key)}</strong>${row.rare ? ' <span class="badge-warn">rare</span>' : ''}</td>
      ${data.splits.map(split => `<td class="num">${fmt(row.counts[split])}<small class="text-muted"> ${pct(row.counts[split], row.total)}</small></td>`).join('')}
      <td class="num"><b>${fmt(row.total)}</b></td>
      <td><span class="split-bar">${data.splits.map(split => row.counts[split] ? `<i title="${split} ${fmt(row.counts[split])}" style="width:${row.counts[split] / row.total * 100}%;background:${SPLIT_COLORS[split]}"></i>` : '').join('')}</span></td>
    </tr>`).join('') : `<tr><td colspan="${data.splits.length + 3}" class="text-muted">Nothing to show.</td></tr>`}
    </tbody></table></div>`
  container.querySelectorAll('[data-split-sort]').forEach(th => th.addEventListener('click', () => {
    renderBreakdownTable(container, data, { rareOnly, sortKey: th.dataset.splitSort })
    if (container.id === 'splits-table') splitsState.sortKey = th.dataset.splitSort
  }))
}

function breakdownCsv(data) {
  const quote = value => `"${String(value).replaceAll('"', '""')}"`
  const lines = [[data.by, ...data.splits, 'total', 'rare'].map(quote).join(',')]
  data.rows.forEach(row => lines.push([row.key, ...data.splits.map(split => row.counts[split]), row.total, row.rare].map(quote).join(',')))
  return lines.join('\n')
}

async function loadSplits() {
  if (!state.projectId) return
  const projectId = state.projectId
  const data = await request(`/api/curation/projects/${projectId}/breakdown?by=${byId('splits-group').value}`)
  if (projectId !== state.projectId) return
  splitsState.data = data
  renderBreakdownTable(byId('splits-table'), data, { rareOnly: byId('splits-rare-only').checked, sortKey: splitsState.sortKey })
}

byId('splits-group').addEventListener('change', () => { splitsState.sortKey = 'total'; loadSplits() })
byId('splits-rare-only').addEventListener('change', () => splitsState.data && renderBreakdownTable(byId('splits-table'), splitsState.data, { rareOnly: byId('splits-rare-only').checked, sortKey: splitsState.sortKey }))
byId('splits-download').addEventListener('click', () => {
  if (!splitsState.data) return
  const link = document.createElement('a')
  link.href = URL.createObjectURL(new Blob([breakdownCsv(splitsState.data)], { type: 'text/csv' }))
  link.download = `splits_by_${splitsState.data.by}.csv`
  link.click()
  URL.revokeObjectURL(link.href)
})

// The check in flight, {projectId, operationId}. It outlives leaving the Status view; the poll timer is its own.
let missingCheck = null
let missingPollTimer = null

function missingCheckButtonState() {
  byId('missing-check').disabled = !canDo('project-curator') || !!(missingCheck && missingCheck.projectId === state.projectId)
}

function pollMissingCheck() {
  clearTimeout(missingPollTimer)
  missingPollTimer = null
  const check = missingCheck
  if (!check) return
  if (check.projectId !== state.projectId) { missingCheck = null; missingCheckButtonState(); return }
  if (state.view !== 'status') return // resumed when Status is shown again
  missingPollTimer = setTimeout(async () => {
    missingPollTimer = null
    if (missingCheck !== check) return
    if (check.projectId !== state.projectId) { missingCheck = null; missingCheckButtonState(); return }
    if (state.view !== 'status') return
    let operation
    try {
      operation = await request(`/api/curation/projects/${check.projectId}/imports/${check.operationId}`)
    } catch (error) {
      if (missingCheck === check) missingCheck = null
      missingCheckButtonState()
      showNotice(error.message)
      return
    }
    if (missingCheck !== check) return
    if (['queued', 'starting', 'running'].includes(operation.status)) { pollMissingCheck(); return }
    missingCheck = null
    missingCheckButtonState()
    if (operation.status !== 'completed') showNotice(`Availability check ${operation.status}${operation.error_message ? `: ${operation.error_message}` : ''}`)
    loadMissingImages().catch(error => showNotice(error.message))
  }, 2000)
}

async function loadMissingImages() {
  const pid = state.projectId
  if (missingCheck && missingCheck.projectId !== pid) { missingCheck = null; clearTimeout(missingPollTimer) }
  missingCheckButtonState()
  const base = `/api/curation/projects/${pid}/availability`
  byId('missing-download').href = `${base}.csv`
  const data = await request(base)
  if (state.projectId !== pid || state.view !== 'status') return
  const rows = data.by_region_year || []
  const checking = missingCheck && missingCheck.projectId === pid ? '<p class="text-muted">Checking the bucket...</p>' : ''
  if (!data.missing_images) {
    byId('missing-summary').innerHTML = checking || '<span class="text-muted">No missing images found (or not checked yet). Use Check again to check now.</span>'
  } else {
    byId('missing-summary').innerHTML = checking + `<p>${Number(data.missing_images).toLocaleString()} source images missing from the bucket (${Number(data.missing_items).toLocaleString()} points)</p>` +
      `<table class="table table-sm w-auto"><thead><tr><th>Region</th><th>Year</th><th class="text-end">Images</th></tr></thead><tbody>` +
      rows.map(row => `<tr><td>${escapeHtml(row.region ?? 'Unknown')}</td><td>${escapeHtml(row.year ?? '')}</td><td class="text-end">${Number(row.images).toLocaleString()}</td></tr>`).join('') +
      '</tbody></table>'
  }
  if (missingCheck) pollMissingCheck()
}

async function checkMissingImages() {
  const pid = state.projectId
  if (!pid || missingCheck) return
  byId('missing-check').disabled = true
  try {
    // The server returns the check already in flight for this project, if there is one.
    const operation = await request(`/api/curation/projects/${pid}/availability`, { method: 'POST' })
    missingCheck = { projectId: pid, operationId: operation.id }
    if (state.projectId === pid && state.view === 'status') byId('missing-summary').innerHTML = '<span class="text-muted">Checking the bucket...</span>'
    missingCheckButtonState()
    pollMissingCheck()
  } catch (error) {
    missingCheckButtonState()
    showNotice(error.message)
  }
}
byId('missing-check').addEventListener('click', checkMissingImages)

async function loadDatasetStatus() {
  loadSplits().catch(error => showNotice(error.message))
  loadMissingImages().catch(error => showNotice(error.message))
  if (typeof fillPeople === 'function') fillPeople().catch(() => {})
  const status = await request(`/api/curation/projects/${state.projectId}/status`)
  const kpis = [
    ['Total available', status.total_items], ['Included in draft', status.included_items],
    ['Excluded', status.excluded_items],
    ['Source images', status.source_images], ['Reviewed', status.reviewed_items],
    ['Classifier predictions', status.classifier_predictions],
  ]
  byId('status-kpis').innerHTML = kpis.map(([label, value]) => `<div><strong>${value.toLocaleString()}</strong><span>${label}</span></div>`).join('')
  const maxLabel = Math.max(1, ...status.labels.map(row => row.total))
  byId('status-labels').innerHTML = status.labels.sort((a, b) => b.total - a.total).map(row => `<div class="status-row"><span><i style="background:${labelColor(row.label)}"></i><b>${escapeHtml(row.label)}</b></span><span class="status-bar"><i style="width:${row.total / maxLabel * 100}%;background:${labelColor(row.label)}"></i></span><span>${row.total.toLocaleString()} available · ${row.included.toLocaleString()} included · ${row.source_images.toLocaleString()} images</span></div>`).join('')
  const maxRegion = Math.max(1, ...status.regions.map(row => row.count))
  byId('status-regions').innerHTML = status.regions.map(row => `<div class="status-row"><span><b>${escapeHtml(row.value)}</b></span><span class="status-bar"><i style="width:${row.count / maxRegion * 100}%"></i></span><span>${row.count.toLocaleString()}</span></div>`).join('')
  await loadActivity(status.curators)
}

const EVENT_LABELS = {
  item_created: 'added', item_updated: 'edited', bulk_item_updated: 'bulk-edited',
  quality_reviewed: 'quality-reviewed', manual_point_created: 'added a point to', source_import_completed: 'finished an import',
}

function describeChange(event) {
  const before = event.previous_value || {}
  const after = event.new_value || {}
  const changes = []
  if (before.current_label && after.current_label && before.current_label !== after.current_label) changes.push(`${before.current_label} → ${after.current_label}`)
  if (before.included !== undefined && after.included !== undefined && before.included !== after.included) changes.push(after.included ? 'included' : 'excluded')
  if (before.review_state && after.review_state && before.review_state !== after.review_state) changes.push(after.review_state)
  if (after.quality_decision) changes.push(`quality ${after.quality_decision}`)
  if (event.event_type === 'source_import_completed') changes.push(`${(after.imported || 0).toLocaleString()} rows added`)
  return changes.join(' · ')
}

async function loadActivity(curators) {
  byId('audit-export').href = `/api/curation/projects/${state.projectId}/events/export`
  const maxChanges = Math.max(1, ...curators.map(row => row.changes))
  byId('status-curators').innerHTML = curators.length
    ? curators.map(row => `<div class="status-row"><span><b>${escapeHtml(row.name)}</b></span><span class="status-bar"><i style="width:${row.changes / maxChanges * 100}%"></i></span><span>${row.changes.toLocaleString()} changes · ${row.items.toLocaleString()} items · ${escapeHtml(timeAgo(row.last_change_at))}</span></div>`).join('')
    : '<div class="empty-state"><strong>No review decisions yet</strong></div>'
  const activity = await request(`/api/curation/projects/${state.projectId}/activity?limit=40`)
  byId('status-activity').innerHTML = activity.events.length
    ? activity.events.map(event => `<li><b>${escapeHtml(event.curator_name)}</b> ${escapeHtml(EVENT_LABELS[event.event_type] || event.event_type)}${event.item_id ? ` item ${event.item_id}` : ''}${describeChange(event) ? ` <span class="activity-change">${escapeHtml(describeChange(event))}</span>` : ''}${event.reason ? ` <q>${escapeHtml(event.reason)}</q>` : ''} <time>${escapeHtml(timeAgo(event.created_at))}</time></li>`).join('')
    : '<li class="empty-state">Nothing has changed yet.</li>'
}

function renderQueue() {
  byId('queue-count').textContent = `${state.images.length} images loaded`
  renderStageNav()
  byId('queue-list').innerHTML = state.images.length
    ? state.images.map(image => `<button class="queue-item source-image-item ${state.currentImage?.first_item_id === image.first_item_id ? 'active' : ''}" data-image-id="${image.first_item_id}">
        <span class="source-image-icon queue-thumb"><i class="bi bi-image"></i>${image.first_item_id ? `<img src="/api/curation/projects/${encodeURIComponent(state.projectId)}/items/${image.first_item_id}/crop-preview" alt="" width="40" height="30" loading="lazy">` : ''}</span>
        <span><strong>${escapeHtml(image.image_name)}</strong><small>${image.outside_filter ? 'Opened from crop · ' : ''}${escapeHtml(image.site || image.region || 'Unknown site')} · ${image.annotation_count} points</small><small>${escapeHtml(image.labels.slice(0, 5).join(', '))}</small><progress class="queue-progress" max="${Math.max(1, image.annotation_count)}" value="${image.reviewed_count}" aria-label="${image.reviewed_count} of ${image.annotation_count} points reviewed"></progress></span>
        <span class="image-review-count">${image.reviewed_count}/${image.annotation_count}</span>
      </button>`).join('')
    : `<div class="queue-empty">${state.sourceQueueLoaded
      ? emptyStateHtml('bi-images', 'No source images match', 'Nothing fits the current selection.', 'queue-clear-filters', 'Clear filters')
      : emptyStateHtml('bi-funnel', 'No images loaded', 'Choose a filter or matrix drilldown to load source images.', 'queue-open-filters', 'Load images')}</div>`
  document.getElementById('queue-clear-filters')?.addEventListener('click', clearFilters)
  document.getElementById('queue-open-filters')?.addEventListener('click', () => byId('filters-button').click())
  // The thumbnail is the patch around the image's first point (cached up to an hour, so it can lag an edit).
  // One that does not load (missing from the bucket) leaves the icon showing.
  byId('queue-list').querySelectorAll('.queue-thumb img').forEach(img => img.addEventListener('error', () => { img.hidden = true }))
  document.querySelectorAll('[data-image-id]').forEach(button => {
    button.addEventListener('click', () => selectImage(Number(button.dataset.imageId)))
  })
}

// ── Queue column collapse (remembered per browser) ──
function setQueueCollapsed(collapsed) {
  byId('curation-workspace').classList.toggle('queue-collapsed', collapsed)
  const toggle = byId('queue-toggle')
  toggle.setAttribute('aria-expanded', String(!collapsed))
  toggle.title = collapsed ? 'Show the queue' : 'Collapse the queue'
  toggle.setAttribute('aria-label', toggle.title)
  toggle.innerHTML = `<i class="bi ${collapsed ? 'bi-chevron-bar-right' : 'bi-chevron-bar-left'}"></i>`
  // The stage changes width without a window resize; the ResizeObserver on the image wrap (init) re-renders the markers.
}
byId('queue-toggle').addEventListener('click', () => {
  const collapsed = !byId('curation-workspace').classList.contains('queue-collapsed')
  setQueueCollapsed(collapsed)
  try { localStorage.setItem('curation-queue-collapsed', collapsed ? '1' : '0') } catch (_) {}
})
// Narrow screens start with the queue collapsed unless the user chose otherwise (the stage keeps the room).
try {
  const storedCollapse = localStorage.getItem('curation-queue-collapsed')
  if (storedCollapse === '1' || (storedCollapse === null && window.matchMedia('(max-width: 900px)').matches)) setQueueCollapsed(true)
} catch (_) {}

async function selectImage(firstItemId, selectedItemId = null) {
  // Unsaved edits on the image now shown are never dropped silently (editor.js).
  if (typeof confirmDiscardPending === 'function' && !(await confirmDiscardPending())) { writeHash(); return false }
  const detail = await request(`/api/curation/projects/${state.projectId}/source-images/${firstItemId}`)
  const existing = state.images.find(image => image.source_image_uri === detail.source_image_uri)
  const summary = existing || {
    first_item_id: detail.first_item_id,
    source_image_uri: detail.source_image_uri,
    image_name: detail.image_name,
    region: detail.region,
    site: detail.site,
    survey_year: detail.survey_year,
    annotation_count: detail.items.length,
    reviewed_count: detail.items.filter(item => item.review_state === 'reviewed').length,
    included_count: detail.items.filter(item => item.included).length,
    labels: [...new Set(detail.items.map(item => item.current_label))].sort(),
    outside_filter: true,
  }
  state.currentImage = { ...summary, first_item_id: detail.first_item_id, width: detail.width, height: detail.height, source_missing: Boolean(detail.source_missing) }
  // Keep the queue order (Arrow stepping relies on it); only an image from outside the list goes on top.
  const at = state.images.findIndex(image => image.source_image_uri === detail.source_image_uri)
  state.images = at >= 0 ? state.images.map((image, index) => index === at ? state.currentImage : image) : [state.currentImage, ...state.images]
  state.items = detail.items
  state.selected = null
  resetImageView()
  byId('source-image-error').classList.add('d-none')
  byId('source-image').classList.remove('d-none')
  const missing = Boolean(detail.source_missing)
  byId('source-missing-notice').classList.toggle('d-none', !missing)
  byId('source-missing-notice').textContent = missing ? `Source image not in the bucket: ${detail.source_image_uri}` : ''
  byId('source-image').src = proxyUrl(detail.source_image_uri, true)
  byId('no-selection').classList.add('d-none')
  byId('paired-view').classList.remove('d-none')
  clearCrop()
  byId('source-location').textContent = `${detail.items.length} annotations`
  renderQueue()
  renderMarkers()
  renderLegend()
  renderAnnotationTable()
  byId('annotation-table-panel').classList.remove('d-none')
  renderImageFacts()
  if (detail.items.length) selectItem(selectedItemId || detail.items[0].id)
  if (typeof claimOpenedImage === 'function') await claimOpenedImage()  // live.js
  return true
}

function selectItem(itemId, { keepTab = false } = {}) {
  state.selected = state.items.find(item => item.id === itemId) || null
  writeHash()
  renderQueue()
  byId('no-selection').classList.toggle('d-none', Boolean(state.selected))
  byId('paired-view').classList.toggle('d-none', !state.selected)
  byId('inspector-empty').classList.toggle('d-none', Boolean(state.selected))
  byId('item-form').classList.toggle('d-none', !state.selected)
  renderModelContext()
  if (!state.selected) return
  // A point picked on the stage opens its Point tab, unless the user is looking at the Model tab;
  // a row picked in the Image tab's annotation table leaves that tab open.
  if (!keepTab && inspectorTab === 'image') setInspectorTab('point')

  const item = state.selected
  byId('source-location').textContent = item.row_px == null ? 'No coordinates' : `${item.column_px}, ${item.row_px}`
  // A dataset patch has no place on a source image: no overlay, no adding points to it.
  const patchOnly = item.row_px == null
  byId('patch-only-label').classList.toggle('d-none', !patchOnly)
  byId('add-point-mode').classList.toggle('d-none', patchOnly)
  byId('overlay-mode').classList.toggle('d-none', patchOnly)
  byId('source-markers').classList.toggle('d-none', patchOnly)
  if (patchOnly && state.addPointMode) {
    state.addPointMode = false
    byId('add-point-mode').classList.remove('active')
  }
  byId('crop-confidence').textContent = item.model_confidence == null ? '' : `${Math.round(item.model_confidence * 100)}%`
  byId('item-region').textContent = [item.region, item.site, item.survey_year].filter(Boolean).join(' · ')
  byId('item-name').textContent = item.file_name
  byId('item-revision').textContent = `r${item.revision}`
  byId('item-last-edit').textContent = item.updated_by_name && item.revision > 1
    ? `Last changed by ${item.updated_by_name}, ${timeAgo(item.updated_at)}`
    : ''
  byId('current-label').value = item.current_label
  byId('source-label').textContent = item.source_label
  renderItemMetadata(item)
  byId('review-state').value = item.review_state
  byId('item-included').checked = item.included
  byId('edit-reason').value = ''
  byId('save-message').textContent = ''
  renderPredictions(item.top_predictions)
  renderMarkers()
  drawSelectedCrop()
  renderAnnotationTable()
  if (typeof syncPointTools === 'function') syncPointTools()
}

function labelColor(label) {
  let hash = 0
  for (const character of String(label || 'UNKNOWN')) hash = ((hash << 5) - hash + character.charCodeAt(0)) | 0
  const hue = Math.abs(hash) % 360
  return `hsl(${hue} 68% 47%)`
}

function renderItemMetadata(item) {
  const metadata = item.metadata || {}
  const values = [
    ['Region', item.region], ['Island', metadata.island], ['Site', item.site],
    ['Year', item.survey_year], ['Survey date', metadata.date],
    ['Coordinates', metadata.latitude && metadata.longitude ? `${metadata.latitude}, ${metadata.longitude}` : null],
    ['Annotator', metadata.annotator], ['Annotated', metadata.date_annotated],
  ].filter(([, value]) => value !== null && value !== undefined && value !== '')
  byId('item-metadata').innerHTML = values.map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd></div>`).join('')
}

function renderLegend() {
  const counts = new Map()
  state.items.forEach(item => counts.set(item.current_label, (counts.get(item.current_label) || 0) + 1))
  byId('point-legend').innerHTML = [...counts].sort((left, right) => right[1] - left[1]).map(([label, count]) => `<span><i style="background:${labelColor(label)}"></i>${escapeHtml(label)} <small>${count}</small></span>`).join('')
}

function renderAnnotationTable() {
  byId('annotation-table-count').textContent = `${state.items.length} points`
  byId('annotation-table-body').innerHTML = state.items.map((item, index) => `<tr class="${state.selected?.id === item.id ? 'table-active' : ''}" data-table-item="${item.id}">
    <td><button type="button" class="table-point-button"><i style="background:${labelColor(item.current_label)}"></i>${index + 1}<small>${item.row_px == null ? 'patch' : `${item.column_px}, ${item.row_px}`}</small></button></td>
    <td><strong>${escapeHtml(item.current_label)}</strong></td><td>${escapeHtml(item.source_label)}</td><td>${escapeHtml(item.model_label || '')}</td>
    <td>${item.model_confidence == null ? '' : `${Math.round(item.model_confidence * 100)}%`}</td><td>${escapeHtml(item.review_state)}</td>
    <td>${item.included ? '<i class="bi bi-check-circle-fill text-success"></i>' : '<i class="bi bi-dash-circle text-danger"></i>'}</td>
    <td>${escapeHtml(item.annotator || item.metadata?.annotator || '')}</td><td>${item.revision > 1 ? `${escapeHtml(item.updated_by_name || '')} · ${escapeHtml(timeAgo(item.updated_at))}` : ''}</td>
  </tr>`).join('')
  document.querySelectorAll('[data-table-item]').forEach(row => row.addEventListener('click', () => selectItem(Number(row.dataset.tableItem), { keepTab: true })))
}

// ── Inspector tabs: Point · Image · Model (the choice is remembered per browser) ──
const INSPECTOR_TABS = ['point', 'image', 'model']
let inspectorTab = 'point'

function setInspectorTab(tab, { remember = false } = {}) {
  if (!INSPECTOR_TABS.includes(tab)) tab = 'point'
  inspectorTab = tab
  document.querySelectorAll('[data-inspector-tab]').forEach(button => {
    const on = button.dataset.inspectorTab === tab
    button.classList.toggle('active', on)
    button.setAttribute('aria-selected', String(on))
  })
  INSPECTOR_TABS.forEach(name => byId(`inspector-${name}`).classList.toggle('d-none', name !== tab))
  // Only a click remembers: the automatic switch to Point on selection must not overwrite the user's choice.
  if (remember) { try { localStorage.setItem('curation-inspector-tab', tab) } catch (_) {} }
  // The stage may change size (the inspector drops below the stage on narrow screens); the ResizeObserver follows.
}
document.querySelectorAll('[data-inspector-tab]').forEach(button => button.addEventListener('click', () => setInspectorTab(button.dataset.inspectorTab, { remember: true })))
try { setInspectorTab(localStorage.getItem('curation-inspector-tab') || 'point') } catch (_) { setInspectorTab('point') }

// Image tab: what this image is, and whether its file is missing (the stage shows the same notice).
function renderImageFacts() {
  const image = state.currentImage
  const target = byId('image-facts')
  if (!image) { target.innerHTML = ''; return }
  const failed = !byId('source-image-error').classList.contains('d-none')
  const values = [
    ['Image', image.image_name], ['Points', state.items.length],
    ['Size', image.width && image.height ? `${image.width} × ${image.height} px` : null],
    ['File', image.source_missing ? 'Not in the bucket' : failed ? 'Could not be loaded' : null],
    ['URI', image.source_image_uri],
  ].filter(([, value]) => value !== null && value !== undefined && value !== '')
  target.innerHTML = values.map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd${key === 'File' ? ' class="is-error"' : ''}>${escapeHtml(value)}</dd></div>`).join('')
}

// Model tab: what the classifier said about the selected point, and the evaluation it was opened from.
let classifiedFor = null

function renderModelContext() {
  const item = state.selected
  const target = byId('model-context')
  if (!byId('point-dialog').open && classifiedFor !== (item?.id ?? null)) {
    byId('classification-results').innerHTML = ''
    byId('point-message').textContent = ''
    classifiedFor = null
  }
  byId('classify-target').textContent = item && item.row_px != null ? `point ${item.column_px}, ${item.row_px}` : 'the selected point'
  if (!item) { target.innerHTML = '<p class="model-note">Select a point to see what the model says about it.</p>'; return }
  const percent = value => value == null ? '' : `${Math.round(Number(value) * 100)}%`
  const facts = [['Model label', item.model_label], ['Confidence', percent(item.model_confidence)], ['Source label', item.source_label], ['Current label', item.current_label]]
    .filter(([, value]) => value !== null && value !== undefined && value !== '')
  const context = evaluationState.editContext?.evidence.curation_link.item_id === item.id ? evaluationState.editContext : null
  const evidence = context?.evidence
  const evaluation = evidence
    ? [['Run', context.runId], ['Split', evidence.split], ['Actual', evidence.actual_label], ['Predicted', evidence.predicted_label], ['Top-1', percent(evidence.top1_confidence)]]
      .filter(([, value]) => value !== null && value !== undefined && value !== '')
    : []
  const list = rows => `<dl class="item-metadata">${rows.map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd></div>`).join('')}</dl>`
  target.innerHTML = `<h3>Classifier</h3>${facts.length ? list(facts) : '<p class="model-note">No model prediction stored for this point.</p>'}
    <h3>Evaluation</h3>${evaluation.length
      ? `${list(evaluation)}${evidence.split === 'test' ? '<p class="model-note">Opened from test evidence: saving a decision asks first, and the original evaluation stays unchanged.</p>' : ''}`
      : '<p class="model-note">Open a crop from the confusion matrix to see its evaluation here.</p>'}`
}

// The classify tools have one home in the Model tab; the add-point dialog borrows them while it is open.
function placeClassifyTools(inDialog) {
  byId(inDialog ? 'point-classify-slot' : 'model-classify-slot').append(byId('classify-tools'))
}
byId('point-dialog').addEventListener('close', () => {
  placeClassifyTools(false)
  byId('classification-results').innerHTML = ''
  byId('point-message').textContent = ''
  classifiedFor = null
})

// ── Image stepping on the stage: ‹ 12 / 3,104 › over the loaded queue ──
function renderStageNav() {
  const total = state.images.length
  const at = state.currentImage ? state.images.findIndex(image => image.first_item_id === state.currentImage.first_item_id) : -1
  const more = state.sourceCursor ? '+' : ''  // the server sends pages, not a total: more pages exist
  byId('stage-position').textContent = at >= 0 ? `${(at + 1).toLocaleString()} / ${total.toLocaleString()}${more}` : `– / ${total.toLocaleString()}${more}`
  byId('stage-prev').disabled = at <= 0
  byId('stage-next').disabled = at < 0 || at >= total - 1
}
// stepImage (editor.js) goes through the unsaved-edits guard.
byId('stage-prev').addEventListener('click', () => { if (typeof stepImage === 'function') stepImage(-1) })
byId('stage-next').addEventListener('click', () => { if (typeof stepImage === 'function') stepImage(1) })

// ── Overlays on the image keep their pointer, click and wheel events: they never pan, zoom or add a point ──
const STAGE_OVERLAYS = '#stage-toolbar, #stage-nav, #pending-tray, .popover'
;['stage-toolbar', 'stage-nav', 'pending-tray', 'pending-review-panel'].forEach(id => {
  ;['pointerdown', 'pointerup', 'click', 'wheel', 'dblclick'].forEach(type => byId(id).addEventListener(type, event => event.stopPropagation()))
  // Keys stay in the overlay too (the filters-popover convention): Delete, Backspace or an arrow on a focused
  // control there never removes, nudges or steps. Ctrl/Cmd shortcuts pass; OF.ui's capture-phase Escape still runs.
  byId(id).addEventListener('keydown', event => { if (!event.ctrlKey && !event.metaKey) event.stopPropagation() })
})

// ── Markers follow the stage size (queue collapse, inspector tabs, window resize), once per frame ──
let stageResizeFrame = 0
function scheduleStageRender() {
  if (stageResizeFrame) return
  stageResizeFrame = requestAnimationFrame(() => {
    stageResizeFrame = 0
    // A re-render mid-drag would replace the marker under the pointer and drop the drag: try again next frame.
    if (typeof markerDragActive === 'function' && markerDragActive()) { scheduleStageRender(); return }
    if (typeof applyImageTransform === 'function') applyImageTransform()
    renderMarkers()
  })
}
function observeStageResize() {
  if (observeStageResize.done || typeof ResizeObserver !== 'function') return
  observeStageResize.done = true
  new ResizeObserver(scheduleStageRender).observe(byId('source-image-wrap'))
  // The workspace (or, on narrow screens, the stage) ends at the bottom of the viewport, so the unsaved-edits
  // pill at the bottom of the stage is in view. CSS reads the workspace's real top offset from --ws-top; it moves
  // when the top bar wraps or a notice, import status or metrics row appears above it.
  const fit = () => fitWorkspaceToViewport()
  const watch = new ResizeObserver(fit)
  watch.observe(document.querySelector('.curation-shell'))
  const topbar = document.querySelector('.topbar')
  if (topbar) watch.observe(topbar)
  window.addEventListener('resize', fit)
}
let fitWorkspaceFrame = 0
function fitWorkspaceToViewport() {
  if (fitWorkspaceFrame) return
  fitWorkspaceFrame = requestAnimationFrame(() => {
    fitWorkspaceFrame = 0
    const workspace = byId('curation-workspace')
    if (workspace.classList.contains('d-none')) return
    const top = Math.round(workspace.getBoundingClientRect().top + window.scrollY)
    if (workspace.style.getPropertyValue('--ws-top') !== `${top}px`) workspace.style.setProperty('--ws-top', `${top}px`)
  })
}

function clearCrop() {
  const canvas = byId('crop-image')
  const context = canvas.getContext('2d')
  context.fillStyle = OF.ui.theme.token('--surface-2') || 'gray'
  context.fillRect(0, 0, canvas.width, canvas.height)
  byId('crop-label').textContent = ''
}

function drawSelectedCrop() {
  const item = state.selected
  const image = byId('source-image')
  if (!item || !image.naturalWidth) {
    clearCrop()
    return
  }
  const canvas = byId('crop-image')
  const context = canvas.getContext('2d')
  context.clearRect(0, 0, canvas.width, canvas.height)
  if (item.column_px == null || item.row_px == null) {
    // A dataset patch has no point: the image on the stage is the patch itself.
    context.drawImage(image, 0, 0, canvas.width, canvas.height)
  } else {
    const geo = markerGeometry(item)  // follows unsaved moves and resizes
    const sourceSize = geo.crop_size || 224
    const sourceX = Math.max(0, Math.min(image.naturalWidth - sourceSize, geo.column_px - sourceSize / 2))
    const sourceY = Math.max(0, Math.min(image.naturalHeight - sourceSize, geo.row_px - sourceSize / 2))
    context.drawImage(image, sourceX, sourceY, sourceSize, sourceSize, 0, 0, canvas.width, canvas.height)
  }
  const color = labelColor(item.current_label)
  byId('crop-preview-wrap').style.setProperty('--label-color', color)
  byId('crop-label').textContent = item.current_label
  byId('crop-label').style.background = color
}

function renderMarkers() {
  const image = byId('source-image')
  const target = byId('source-markers')
  if (!state.items.length || !image.naturalWidth) { target.innerHTML = ''; return }
  const wrap = byId('source-image-wrap')
  const scale = Math.min(wrap.clientWidth / image.naturalWidth, wrap.clientHeight / image.naturalHeight)
  const renderedWidth = image.naturalWidth * scale
  const renderedHeight = image.naturalHeight * scale
  const scene = byId('source-scene')
  const centeredX = (wrap.clientWidth - renderedWidth) / 2
  const centeredY = (wrap.clientHeight - renderedHeight) / 2
  scene.style.width = `${renderedWidth}px`
  scene.style.height = `${renderedHeight}px`
  scene.style.left = `${centeredX}px`
  scene.style.top = `${centeredY}px`
  scene.style.position = 'relative'
  state.markerScale = scale
  const patchMode = byId('overlay-mode').value === 'patch'
  const markers = state.items.filter(item => item.column_px != null && item.row_px != null).map(item => {
    // Where the point will be once its unsaved edits (editor.js) are saved.
    const geo = markerGeometry(item)
    const selected = state.selected?.id === item.id
    const editable = selected && typeof markerEditable === 'function' && markerEditable(item)
    const square = patchMode || editable  // the patch is always visible while editing it
    const title = `${item.current_label} (${geo.column_px}, ${geo.row_px})${geo.removed ? ' — removed' : ''}`
    const patchSize = Math.max(12, Number(geo.crop_size) * scale)
    const color = labelColor(item.current_label)
    const handle = editable ? `<span class="resize-handle" data-resize="${item.id}" title="Drag to resize the patch" style="border-color:${color}"></span>` : ''
    return `<button type="button" class="source-marker ${square ? 'patch' : ''} ${selected ? 'selected' : ''} ${item.included ? '' : 'excluded'} ${geo.removed ? 'removed' : ''}" data-point-id="${item.id}" title="${escapeHtml(title)}" style="left:${Number(geo.column_px) * scale}px;top:${Number(geo.row_px) * scale}px;background:${square ? 'transparent' : color};border-color:${color};${square ? `width:${patchSize}px;height:${patchSize}px` : ''}"><span>${escapeHtml(item.current_label)}</span>${handle}</button>`
  })
  // New points not saved yet: dashed, with no item behind them.
  const added = (state.pending?.added || []).map((point, index) => {
    const size = Math.max(12, Number(point.crop_size) * scale)
    const color = labelColor(point.label)
    return `<button type="button" class="source-marker patch pending" data-pending-index="${index}" title="${escapeHtml(`New ${point.label} (${point.column_px}, ${point.row_px}) — not saved`)}" style="left:${Number(point.column_px) * scale}px;top:${Number(point.row_px) * scale}px;border-color:${color};width:${size}px;height:${size}px"><span>${escapeHtml(point.label)}</span></button>`
  })
  target.innerHTML = [...markers, ...added].join('')
  target.querySelectorAll('[data-point-id]').forEach(button => button.addEventListener('click', () => {
    // A click on the point already selected (the end of a drag, too) changes nothing.
    if (Number(button.dataset.pointId) !== state.selected?.id) selectItem(Number(button.dataset.pointId))
  }))
  if (typeof wireMarkerEditing === 'function') wireMarkerEditing(target)
  drawSelectedCrop()
  applyImageTransform()
  if (typeof renderPending === 'function') renderPending()
}

function markerGeometry(item) {
  return typeof effectiveGeometry === 'function'
    ? effectiveGeometry(item)
    : { row_px: item.row_px, column_px: item.column_px, crop_size: item.crop_size, removed: Boolean(item.removed) }
}

function applyImageTransform() {
  const { zoom, panX, panY } = state.imageView
  const transform = `translate(${panX}px, ${panY}px) scale(${zoom})`
  byId('source-scene').style.transform = transform
  byId('zoom-level').textContent = `${Math.round(zoom * 100)}%`
}

function resetImageView() {
  Object.assign(state.imageView, { zoom: 1, panX: 0, panY: 0, dragging: false })
  applyImageTransform()
}

function zoomImage(factor) {
  state.imageView.zoom = Math.max(0.5, Math.min(12, state.imageView.zoom * factor))
  applyImageTransform()
}

async function toggleImageFullscreen() {
  const wrap = byId('source-image-wrap')
  if (document.fullscreenElement === wrap) await document.exitFullscreen()
  else await wrap.requestFullscreen()
}

function eventToImagePoint(event) {
  const wrap = byId('source-image-wrap')
  const image = byId('source-image')
  if (!image.naturalWidth) return null
  const rect = wrap.getBoundingClientRect()
  const centerX = rect.width / 2
  const centerY = rect.height / 2
  const baseX = ((event.clientX - rect.left) - centerX - state.imageView.panX) / state.imageView.zoom + centerX
  const baseY = ((event.clientY - rect.top) - centerY - state.imageView.panY) / state.imageView.zoom + centerY
  const fit = Math.min(rect.width / image.naturalWidth, rect.height / image.naturalHeight)
  const offsetX = (rect.width - image.naturalWidth * fit) / 2
  const offsetY = (rect.height - image.naturalHeight * fit) / 2
  const column = Math.round((baseX - offsetX) / fit)
  const row = Math.round((baseY - offsetY) / fit)
  if (column < 0 || row < 0 || column >= image.naturalWidth || row >= image.naturalHeight) return null
  return { row, column }
}

function openPointDialog(point) {
  byId('point-row').value = point.row
  byId('point-column').value = point.column
  byId('point-label').value = state.selected?.current_label || ''
  placeClassifyTools(true)  // borrowed from the Model tab until the dialog closes
  byId('classification-results').innerHTML = ''
  byId('point-message').textContent = ''
  byId('point-dialog').showModal()
}

async function addManualPoint(event) {
  event.preventDefault()
  if (!state.currentImage) return
  // Adding reloads the image, so settle unsaved edits before anything is written.
  if (typeof confirmDiscardPending === 'function' && !(await confirmDiscardPending())) return
  const created = await request(`/api/curation/projects/${state.projectId}/source-images/${state.currentImage.first_item_id}/points`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
      row_px: Number(byId('point-row').value), column_px: Number(byId('point-column').value),
      crop_size: Number(byId('point-size').value), label: byId('point-label').value.trim(),
      reason: byId('point-reason').value.trim() || null,
    }),
  })
  byId('point-dialog').close()
  state.addPointMode = false
  byId('add-point-mode').classList.remove('active')
  await selectImage(state.currentImage.first_item_id, created.id)
  await loadStats()
}

// Where to classify: the dialog's point while adding one, otherwise the selected point as shown (unsaved edits included).
function classifyTarget() {
  if (byId('point-dialog').open) {
    return { inDialog: true, row: Number(byId('point-row').value), column: Number(byId('point-column').value), size: Number(byId('point-size').value) }
  }
  const item = state.selected
  if (!item || item.row_px == null || !state.currentImage) return null
  const geo = markerGeometry(item)
  return { inDialog: false, itemId: item.id, row: Number(geo.row_px), column: Number(geo.column_px), size: Number(geo.crop_size) || 224 }
}

async function classifyPoint() {
  if (!byId('point-model').value) { byId('point-message').textContent = 'Choose a classification model first.'; return }
  const target = classifyTarget()
  if (!target) { byId('point-message').textContent = 'Select a point on the source image first.'; return }
  const projectId = state.projectId
  const image = state.currentImage
  byId('point-message').textContent = 'Classifying patches...'
  try {
    const result = await request(`/api/curation/projects/${projectId}/source-images/${image.first_item_id}/classify`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
        row_px: target.row, column_px: target.column,
        crop_size: target.size, model_name: byId('point-model').value,
        radius: Number(byId('dense-radius').value), step: Number(byId('dense-step').value),
      }),
    })
    // The reply is for the point it was asked about: drop it if the page moved on meanwhile.
    if (state.projectId !== projectId || state.currentImage !== image || byId('point-dialog').open !== target.inDialog
      || (!target.inDialog && state.selected?.id !== target.itemId)) return
    classifiedFor = target.inDialog ? null : target.itemId
    byId('classification-results').innerHTML = result.points.map(point => {
      const top = point.predictions[0]
      return `<button type="button" data-classified-row="${point.row_px}" data-classified-column="${point.column_px}" data-classified-label="${escapeHtml(top?.label || '')}"><strong>${escapeHtml(top?.label || '?')}</strong><span>${top ? Math.round(top.confidence * 100) : 0}%</span><small>${point.column_px}, ${point.row_px}</small></button>`
    }).join('')
    document.querySelectorAll('[data-classified-row]').forEach(button => button.addEventListener('click', () => {
      if (byId('point-dialog').open) {
        byId('point-row').value = button.dataset.classifiedRow
        byId('point-column').value = button.dataset.classifiedColumn
        byId('point-label').value = button.dataset.classifiedLabel
        return
      }
      // In the Model tab a result relabels the selected point; the decision is saved from the Point tab as usual.
      if (!button.dataset.classifiedLabel || !state.selected) return
      byId('current-label').value = button.dataset.classifiedLabel
      byId('point-message').textContent = `Label set to ${button.dataset.classifiedLabel}: save the decision on the Point tab to keep it.`
    }))
    byId('point-message').textContent = `${result.points.length} patch predictions ready.`
  } catch (error) { byId('point-message').textContent = error.message }
}

async function loadModels() {
  const result = await request('/api/curation/classification-models')
  state.models = result.models
  byId('point-model').innerHTML = '<option value="">Select classification model</option>' + state.models.map(model => `<option value="${escapeHtml(model.name)}">${escapeHtml(model.name)}</option>`).join('')
}

function renderPredictions(predictions) {
  const target = byId('prediction-list')
  const displayPredictions = projectPredictions(predictions || [])
  target.innerHTML = displayPredictions.length
    ? `<p>Model suggestions</p>${displayPredictions.map(prediction => `<button type="button" class="prediction" data-label="${escapeHtml(prediction.label)}"><strong>${escapeHtml(prediction.label)}</strong><span>${Math.round((prediction.confidence || 0) * 100)}%</span></button>`).join('')}`
    : ''
  target.querySelectorAll('[data-label]').forEach(button => {
    button.addEventListener('click', () => { byId('current-label').value = button.dataset.label })
  })
}

function projectPredictions(predictions) {
  if ((currentProject()?.dataset_mode || 'T3') !== 'T1') return predictions
  const projected = new Map()
  predictions.forEach(prediction => {
    const node = state.taxonomy.find(candidate => candidate.tier === 'TIER_1' && candidate.codes.includes(prediction.label))
    const label = node?.code || 'UC'
    const current = projected.get(label)
    if (!current || (prediction.confidence || 0) > (current.confidence || 0)) {
      projected.set(label, { ...prediction, label })
    }
  })
  return [...projected.values()].sort((left, right) => (right.confidence || 0) - (left.confidence || 0))
}

async function loadStats() {
  if (!state.projectId) return
  const projectId = state.projectId
  const panel = byId('metric-total').closest('section')
  panel.setAttribute('aria-busy', 'true')
  ;['metric-total', 'metric-included', 'metric-reviewed', 'metric-flagged', 'metric-progress'].forEach(id => { byId(id).textContent = '...' })
  const stats = await request(`/api/curation/projects/${projectId}/stats`)
  if (projectId !== state.projectId) return
  byId('metric-total').textContent = stats.total.toLocaleString()
  byId('metric-included').textContent = stats.included.toLocaleString()
  byId('metric-reviewed').textContent = stats.reviewed.toLocaleString()
  byId('metric-flagged').textContent = stats.flagged.toLocaleString()
  byId('metric-progress').textContent = stats.total ? `${Math.round(stats.reviewed / stats.total * 100)}%` : '0%'
  panel.setAttribute('aria-busy', 'false')
  if (typeof refreshCommitPill === 'function') refreshCommitPill()
}

async function saveItem(event) {
  event?.preventDefault()
  const item = state.selected
  if (!item) return false
  try {
    const context = evaluationState.editContext?.evidence.curation_link.item_id === item.id ? evaluationState.editContext : null
    const testInformed = context?.evidence.split === 'test'
    if (testInformed && !(await window.OF.ui.confirm({
      title: 'Apply this test-informed correction?',
      message: 'It goes to the draft for a future version. The original evaluation will remain unchanged.',
      confirmLabel: 'Apply correction',
    }))) return false
    const evidencePath = context?.evidence.prediction_id ? `predictions/${context.evidence.prediction_id}` : `inputs/${context?.evidence.input_item_id}`
    const url = context ? `/api/curation/projects/${state.projectId}/evaluations/${context.runId}/${evidencePath}/item`
      : `/api/curation/projects/${state.projectId}/items/${item.id}`
    const updated = await request(url, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        expected_revision: item.revision,
        confirm_test: !!testInformed,
        split: context?.evidence.split || 'test',
        current_label: byId('current-label').value.trim(),
        included: byId('item-included').checked,
        review_state: byId('review-state').value,
        reason: byId('edit-reason').value.trim() || null,
      }),
    })
    state.items = state.items.map(row => row.id === updated.id ? updated : row)
    state.selected = updated
    // selectItem() clears the message, so say "saved" after it, not before.
    selectItem(updated.id)
    byId('save-message').textContent = 'Decision saved.'
    byId('save-message').className = 'small text-success'
    if (state.currentImage) {
      const reviewed = state.items.filter(row => row.review_state === 'reviewed').length
      state.currentImage.reviewed_count = reviewed
    }
    // The counts refresh in the background: Save & next moves on without waiting for them.
    loadStats().catch(error => showNotice(error.message))
    return true
  } catch (error) {
    if (error.status === 409 && state.currentImage) {
      // Someone else saved first. Show their version rather than keep a
      // stale form, and keep what this curator typed so it can be re-applied.
      const attempted = byId('current-label').value.trim()
      const shown = await selectImage(state.currentImage.first_item_id, item.id)
      const who = error.detail?.updated_by_name || 'Someone else'
      byId('save-message').textContent = shown
        ? `${who} changed this item while you had it open, so your save was not applied. Their version is shown now; you had entered “${attempted}”.`
        : `${who} changed this item while you had it open, so your save was not applied. Reload the image to see their version; you had entered “${attempted}”.`
    } else {
      byId('save-message').textContent = error.message
    }
    byId('save-message').className = 'small text-danger'
    return false
  }
}

// Save, then move on: the next point on this image, else the next image in the queue. *changes* sets
// the review state or inclusion first (the A, F and X keys).
async function saveAndNext(changes = {}) {
  const item = state.selected
  if (!item || !canDo('project-curator')) return
  if (changes.review_state) byId('review-state').value = changes.review_state
  if (changes.included != null) byId('item-included').checked = changes.included
  if (!(await saveItem())) return
  const next = state.items[state.items.findIndex(row => row.id === item.id) + 1]
  if (next) selectItem(next.id)
  else if (typeof stepImage === 'function') {
    await stepImage(1)
    if (typeof skipHeldImages === 'function') await skipHeldImages(1)  // past images another curator has open
  }
}

async function createProject(event) {
  event.preventDefault()
  if (typeof confirmDiscardPending === 'function' && !(await confirmDiscardPending())) return
  byId('project-message').textContent = ''
  let created
  try {
    created = await request('/api/curation/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: byId('project-name').value.trim(),
        description: byId('project-description').value.trim() || null,
        dataset_mode: byId('project-mode').value,
      }),
    })
  } catch (error) {
    byId('project-message').textContent = error.message
    return
  }
  byId('project-dialog').close()
  event.target.reset()
  state.projectId = created.id
  await loadProjects()
}

function escapeHtml(value) {
  // Quotes too: callers also interpolate into attribute values.
  const div = document.createElement('div')
  div.textContent = value ?? ''
  return div.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;')
}

document.querySelectorAll('[data-create-project]').forEach(button => button.addEventListener('click', () => byId('project-dialog').showModal()))
byId('new-project-button').addEventListener('click', () => byId('project-dialog').showModal())
byId('import-noaa-button').addEventListener('click', startNoaaImport)
byId('cancel-import').addEventListener('click', cancelImport)
byId('close-project-dialog').addEventListener('click', () => byId('project-dialog').close())
byId('project-form').addEventListener('submit', createProject)
byId('item-form').addEventListener('submit', saveItem)
byId('save-next').addEventListener('click', () => saveAndNext())
// Review keys on the source view: A accept, F flag, X exclude (each saves and moves on), R relabel
// (focuses the label). Shift+Enter in the form saves and moves on. Text fields keep their own keys.
document.addEventListener('keydown', event => {
  if (state.view !== 'source' || !state.selected || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return
  if (document.querySelector('dialog[open]')) return
  if (event.shiftKey && event.key === 'Enter' && event.target.closest?.('#item-form')) {
    event.preventDefault()
    saveAndNext()
    return
  }
  if (event.shiftKey || event.repeat || event.target.closest?.('input, textarea, select, [contenteditable]:not([contenteditable="false"])')) return
  const key = event.key.toLowerCase()
  const actions = { a: { review_state: 'reviewed', included: true }, f: { review_state: 'flagged' }, x: { review_state: 'reviewed', included: false } }
  if (actions[key]) {
    event.preventDefault()
    saveAndNext(actions[key])
  } else if (key === 'r' && canDo('project-curator')) {
    event.preventDefault()
    byId('current-label').focus()
    byId('current-label').select()
  }
})
byId('source-image').addEventListener('load', () => {
  byId('source-image-error').classList.add('d-none')
  byId('source-image').classList.remove('d-none')
  renderMarkers()
})
byId('source-image').addEventListener('error', () => {
  if (state.currentImage) {
    byId('source-missing-notice').classList.remove('d-none')
    byId('source-missing-notice').textContent = `Source image not in the bucket: ${state.currentImage.source_image_uri}`
  }
  byId('source-image').classList.add('d-none')
  byId('source-image-error').classList.remove('d-none')
  byId('source-markers').innerHTML = ''
  clearCrop()
  renderImageFacts()
})
byId('apply-filters').addEventListener('click', () => { addCodeFilters(); cascadeFilters() })
byId('apply-filters').addEventListener('click', () => { OF.ui.closePopover(); byId('filters-button').focus() })
byId('refresh-queue').addEventListener('click', () => loadQueue(false))
byId('load-more').addEventListener('click', () => loadQueue(true))
byId('source-view-button').addEventListener('click', () => setView('source'))
byId('crop-view-button').addEventListener('click', () => setView('crop'))
byId('status-view-button').addEventListener('click', () => setView('status'))
byId('matrix-view-button').addEventListener('click', () => setView('matrix'))
byId('versions-view-button').addEventListener('click', () => setView('versions'))
byId('activity-view-button').addEventListener('click', () => setView('activity'))
byId('refresh-preview').addEventListener('click', loadVersions)
byId('publish-form').addEventListener('submit', publishVersion)
byId('refresh-crops').addEventListener('click', () => loadCrops(false))
byId('load-more-crops').addEventListener('click', () => loadCrops(true))
// The floating selection bar: quick actions set the same controls Apply reads, so there is one code path.
function barAction(prefix, action, apply) {
  const select = byId(prefix + 'bulk-action')
  select.value = action
  select.dispatchEvent(new Event('change'))
  if (action === 'relabel') byId(prefix + 'bulk-label').focus()
  else apply()
}
byId('crop-bar-include').addEventListener('click', () => barAction('', 'include', applyBulkAction))
byId('crop-bar-exclude').addEventListener('click', () => barAction('', 'exclude', applyBulkAction))
byId('crop-bar-relabel').addEventListener('click', () => barAction('', 'relabel', applyBulkAction))
byId('crop-bar-clear').addEventListener('click', () => { state.selectedCropIds.clear(); renderCropGrid() })
byId('matrix-bar-include').addEventListener('click', () => barAction('matrix-', 'include', applyMatrixBulkAction))
byId('matrix-bar-exclude').addEventListener('click', () => barAction('matrix-', 'exclude', applyMatrixBulkAction))
byId('matrix-bar-relabel').addEventListener('click', () => barAction('matrix-', 'relabel', applyMatrixBulkAction))
byId('matrix-bar-clear').addEventListener('click', () => { state.selectedMatrixItemIds.clear(); renderEvaluationGrid() })
byId('select-page-crops').addEventListener('click', () => { updateSelectionState(state.selectedCropIds, state.crops.map(item => item.id), 'select'); renderCropGrid() })
byId('select-visible-crops').addEventListener('click', () => { updateSelectionState(state.selectedCropIds, state.crops.map(item => item.id), 'select'); renderCropGrid() })
byId('clear-visible-crops').addEventListener('click', () => { updateSelectionState(state.selectedCropIds, state.crops.map(item => item.id), 'deselect'); renderCropGrid() })
byId('invert-visible-crops').addEventListener('click', () => { updateSelectionState(state.selectedCropIds, state.crops.map(item => item.id), 'toggle'); renderCropGrid() })
byId('lasso-crops').addEventListener('click', () => toggleLasso('crop'))
byId('select-all-crops').addEventListener('click', () => { state.selectedCropIds = new Set(state.selectedCropIds); state.crops.forEach(item => state.selectedCropIds.add(item.id)); renderCropGrid() })
byId('clear-all-crops').addEventListener('click', () => { state.selectedCropIds.clear(); renderCropGrid() })
byId('bulk-action').addEventListener('change', updateBulkControls)
byId('crop-grid-size').addEventListener('change', event => { state.cropGridSize = event.target.value; applyGridSizing(); renderCropGrid() })
byId('apply-bulk').addEventListener('click', applyBulkAction)
byId('bulk-label').addEventListener('input', updateBulkControls)
byId('matrix-actual-axis').addEventListener('change', loadConfusionMatrix)
byId('matrix-source').addEventListener('change', () => {
  changeEvaluationSource()
  writeHash()
})
byId('evaluation-split').addEventListener('change', changeEvaluationSource)
byId('evaluation-run-form').addEventListener('submit', startEvaluation)
byId('evaluation-model-form').addEventListener('submit', registerEvaluationModel)
byId('matrix-normalization').addEventListener('change', loadConfusionMatrix)
byId('matrix-class-order').addEventListener('change', renderConfusionMatrix)
byId('matrix-search').addEventListener('input', renderConfusionMatrix)
byId('refresh-matrix').addEventListener('click', loadConfusionMatrix)
byId('matrix-zoom-in').addEventListener('click', () => zoomMatrix(1.2))
byId('matrix-zoom-out').addEventListener('click', () => zoomMatrix(1 / 1.2))
byId('matrix-fit').addEventListener('click', fitMatrixToWidth)
byId('matrix-reset-zoom').addEventListener('click', resetMatrixZoom)
byId('clear-matrix-drilldown').addEventListener('click', clearMatrixDrilldown)
byId('matrix-to-quality').addEventListener('click', openMatrixInQuality)
byId('matrix-cleanup').addEventListener('click', () => openCleanup().catch(error => showNotice(error.message)))
document.querySelectorAll('[data-cleanup-split]').forEach(box => box.addEventListener('change',
  () => loadCleanupPreview().catch(error => { byId('cleanup-preview').textContent = error.message })))
document.querySelectorAll('[data-cleanup-action]').forEach(button => button.addEventListener('click',
  () => applyCleanup(button.dataset.cleanupAction)))
byId('close-evaluation-drilldown').addEventListener('click', clearMatrixDrilldown)
byId('load-more-evaluation').addEventListener('click', () => loadMatrixAssets(true))
document.querySelectorAll('[data-matrix-split]').forEach(button => button.addEventListener('click', async () => {
  state.evaluationSplit = button.dataset.matrixSplit
  state.evaluationCursor = null
  state.selectedMatrixItemIds.clear()
  if (byId('evaluation-evidence-kind').value === 'predictions') {
    byId('evaluation-split').value = state.evaluationSplit
    rememberEvaluation()
    await loadConfusionMatrix()
  }
  if (byId('evaluation-evidence-kind').value === 'inventory' && state.matrixFilter && state.evaluationSplit !== 'test') {
    const mode = state.matrixClassModes[state.evaluationSplit]
    if (mode === 'actual') state.selectedMatrixClasses = new Set([state.matrixFilter.actual].filter(Boolean))
    else if (mode === 'predicted') state.selectedMatrixClasses = new Set([state.matrixFilter.predicted].filter(Boolean))
    else state.selectedMatrixClasses = new Set([state.matrixFilter.actual, state.matrixFilter.predicted].filter(Boolean))
  }
  await loadMatrixAssets(false)
}))
byId('evaluation-evidence-kind').addEventListener('change', async () => {
  if (byId('evaluation-evidence-kind').value === 'inventory' && !selectedRunId()) state.evaluationSplit = 'train'
  else if (byId('evaluation-evidence-kind').value === 'predictions') state.evaluationSplit = byId('evaluation-split').value
  state.evaluationCursor = null
  state.selectedMatrixItemIds.clear()
  await loadMatrixAssets(false)
})
byId('matrix-class-mode').addEventListener('change', applyMatrixClassMode)
byId('matrix-evidence-sort').addEventListener('change', () => {
  state.evaluationSort = byId('matrix-evidence-sort').value
  state.evaluationCursor = null
  loadMatrixAssets(false)
})
byId('select-matrix-page').addEventListener('click', () => {
  const ids = state.evaluationItems.filter(item => item.mutable && item.curation_link.item_id).map(item => item.curation_link.item_id)
  updateSelectionState(state.selectedMatrixItemIds, ids, 'select')
  renderEvaluationGrid()
})
byId('select-visible-matrix').addEventListener('click', () => {
  const ids = state.evaluationItems.filter(item => item.mutable && item.curation_link.item_id).map(item => item.curation_link.item_id)
  updateSelectionState(state.selectedMatrixItemIds, ids, 'select')
  renderEvaluationGrid()
})
byId('clear-visible-matrix').addEventListener('click', () => {
  const ids = state.evaluationItems.filter(item => item.mutable && item.curation_link.item_id).map(item => item.curation_link.item_id)
  updateSelectionState(state.selectedMatrixItemIds, ids, 'deselect')
  renderEvaluationGrid()
})
byId('invert-visible-matrix').addEventListener('click', () => {
  const ids = state.evaluationItems.filter(item => item.mutable && item.curation_link.item_id).map(item => item.curation_link.item_id)
  updateSelectionState(state.selectedMatrixItemIds, ids, 'toggle')
  renderEvaluationGrid()
})
byId('lasso-matrix').addEventListener('click', () => toggleLasso('matrix'))
byId('select-all-matrix').addEventListener('click', () => {
  state.evaluationItems.filter(item => item.mutable && item.curation_link.item_id).forEach(item => state.selectedMatrixItemIds.add(item.curation_link.item_id))
  renderEvaluationGrid()
})
byId('clear-all-matrix').addEventListener('click', () => { state.selectedMatrixItemIds.clear(); renderEvaluationGrid() })
byId('matrix-grid-size').addEventListener('change', event => { state.matrixGridSize = event.target.value; applyGridSizing(); renderEvaluationGrid() })
byId('matrix-bulk-action').addEventListener('change', updateMatrixBulkControls)
byId('matrix-bulk-label').addEventListener('input', updateMatrixBulkControls)
byId('apply-matrix-bulk').addEventListener('click', applyMatrixBulkAction)
byId('project-select').addEventListener('change', event => { state.projectSwitch = switchProject(event.target.value); return state.projectSwitch })
// Resolves true once the project is shown, or false when the unsaved-edits guard keeps the current one
// (the select, hash and pending link are put back).
async function switchProject(projectId) {
  if (typeof confirmDiscardPending === 'function' && !(await confirmDiscardPending())) {
    byId('project-select').value = state.projectId
    state.pendingView = null
    state.pendingRun = null
    state.pendingImage = null
    writeHash()
    return false
  }
  state.projectId = projectId
  byId('project-select').value = projectId  // palette jumps reach here without the select's own change
  state.backTo = null
  try { localStorage.setItem('curation-project', state.projectId) } catch (_) {}
  state.resolvedCropUris.clear()
  state.matrixFilter = null
  state.matrixLinkedItemIds = []
  byId('compare-result').innerHTML = ''
  evaluationState.editContext = null
  resetSourceWorkspace()
  renderProjectMode()
  renderTaxonomyOptions()
  if (typeof resetChangeFeed === 'function') await resetChangeFeed()  // live.js: before any list loads
  await loadEvaluationCatalog()
  setView(state.pendingView || 'matrix')
  state.pendingView = null
  Promise.all([loadFacets(), loadStats(), loadLatestImport()]).catch(error => showNotice(error.message))
  const pendingImage = state.pendingImage
  state.pendingImage = null
  await applyLinkedImage(pendingImage)
  renderFilterChipBar()
  return true
}
byId('add-taxonomy').addEventListener('click', addTaxonomyFilter)
byId('taxonomy-filter').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); addTaxonomyFilter(); cascadeFilters() } })
byId('add-code').addEventListener('click', addCodeFilters)
byId('filter-label').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); addCodeFilters(); cascadeFilters() } })
byId('filter-image').addEventListener('keydown', event => { if (event.key === 'Enter') { applyActiveFilters(); renderFilterChipBar() } })
byId('clear-filters').addEventListener('click', clearFilters)
byId('export-button').addEventListener('click', exportFiltered)
byId('delete-project-button').addEventListener('click', deleteProject)
;['filter-region', 'filter-island', 'filter-year', 'filter-site', 'filter-annotator', 'filter-origin', 'filter-state', 'filter-included', 'filter-split', 'filter-hide-missing', 'filter-show-removed', 'filter-hide-handled', 'filter-hide-claimed'].forEach(id => byId(id).addEventListener('change', cascadeFilters))
byId('overlay-mode').addEventListener('change', renderMarkers)
byId('add-point-mode').addEventListener('click', () => { state.addPointMode = !state.addPointMode; byId('add-point-mode').classList.toggle('active', state.addPointMode) })
byId('close-point-dialog').addEventListener('click', () => byId('point-dialog').close())
byId('point-form').addEventListener('submit', addManualPoint)
byId('classify-point').addEventListener('click', classifyPoint)
byId('zoom-in').addEventListener('click', () => zoomImage(1.25))
byId('zoom-out').addEventListener('click', () => zoomImage(0.8))
byId('fit-image').addEventListener('click', resetImageView)
byId('fullscreen-image').addEventListener('click', toggleImageFullscreen)
// The image's own handlers skip markers and the overlays (toolbar, stepping, the pill, the Review popover).
byId('source-image-wrap').addEventListener('wheel', event => {
  if (event.target.closest(STAGE_OVERLAYS)) return
  event.preventDefault()
  zoomImage(event.deltaY < 0 ? 1.15 : 1 / 1.15)
}, { passive: false })
byId('source-image-wrap').addEventListener('pointerdown', event => {
  if (event.target.closest('.source-marker')) return
  if (event.target.closest(STAGE_OVERLAYS)) return
  state.imageView.dragging = true
  state.imageView.moved = false
  state.imageView.startX = event.clientX - state.imageView.panX
  state.imageView.startY = event.clientY - state.imageView.panY
  byId('source-image-wrap').setPointerCapture(event.pointerId)
})
byId('source-image-wrap').addEventListener('pointermove', event => {
  if (!state.imageView.dragging) return
  state.imageView.panX = event.clientX - state.imageView.startX
  state.imageView.panY = event.clientY - state.imageView.startY
  state.imageView.moved = true
  applyImageTransform()
})
byId('source-image-wrap').addEventListener('pointerup', event => {
  state.imageView.dragging = false
  if (event.target.closest('.source-marker')) return
  if (event.target.closest(STAGE_OVERLAYS)) return
  if (state.addPointMode && !state.imageView.moved) {
    const point = eventToImagePoint(event)
    if (point) openPointDialog(point)
  }
})
byId('source-image-wrap').addEventListener('dblclick', event => { if (!event.target.closest(STAGE_OVERLAYS)) resetImageView() })
document.addEventListener('fullscreenchange', () => {
  byId('fullscreen-image').innerHTML = document.fullscreenElement === byId('source-image-wrap') ? '<i class="bi bi-fullscreen-exit"></i>' : '<i class="bi bi-fullscreen"></i>'
  renderMarkers()
})
// Window resizes, queue collapse and inspector tab changes all reach the markers through the ResizeObserver set up in init().
// A link, not openSource, moved us: there is nowhere to go back to.
function leftByLink() {
  state.backTo = null
  renderBackBar()
}
window.addEventListener('hashchange', async () => {
  const linked = readHash()
  if (linked.project && linked.project !== state.projectId && state.projects.some(project => project.id === linked.project)) {
    state.pendingView = linked.view
    state.pendingRun = linked.run
    state.pendingImage = linked
    const select = byId('project-select')
    select.value = linked.project
    select.dispatchEvent(new Event('change'))
  } else if (linked.view && linked.view !== state.view) {
    const shown = setView(linked.view)
    // With no unsaved edits the view has already switched; otherwise wait for the curator's answer.
    if (state.view !== linked.view) await shown
    if (state.view === linked.view) {  // not refused by the unsaved-edits guard
      leftByLink()
      if (!linked.project || linked.project === state.projectId) applyLinkedImage(linked)
    }
  } else if (linked.view === 'source' && linked.image && (!linked.project || linked.project === state.projectId) && linked.image !== state.currentImage?.first_item_id) {
    applyLinkedImage(linked).then(() => { if (state.currentImage?.first_item_id === linked.image) leftByLink() })
  } else if (linked.view === 'source' && linked.item && linked.item !== state.selected?.id && state.items.some(item => item.id === linked.item)) {
    selectItem(linked.item)
    leftByLink()
  }
})

byId('curation-notice-close').addEventListener('click', () => byId('curation-notice').classList.add('d-none'))
// Any failed action that is not handled where it happened still says what went wrong.
window.addEventListener('unhandledrejection', event => {
  if (event.reason instanceof Error) showNotice(event.reason.message)
})

async function init() {
  observeStageResize()
  const me = await request('/api/auth/me')
  session.user = me.user
  session.authEnabled = me.auth_enabled
  if (typeof watchRegistrations === 'function') watchRegistrations()  // hub.js: now that we know who is signed in
  applyRoleGates()
  const linked = readHash()
  state.pendingView = linked.view
  state.pendingRun = linked.run
  try { state.projectId = linked.project || localStorage.getItem('curation-project') } catch (_) { state.projectId = linked.project }
  await Promise.all([loadTaxonomy(), loadModels(), loadProjects()])
  state.backTo = null
  if (linked.project && linked.project === state.projectId) await applyLinkedImage(linked)
}

init().catch(error => showNotice(error.message))
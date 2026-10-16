'use strict'
// Hugging Face registry: look up, register, create or link a project, register a model.
// A classic script after curation.js, so it shares that file's request/byId/escapeHtml/
// canDo/state/loadProjects/loadEvaluationCatalog/currentProject/renderImport/showNotice.

// A dataset goes Look up -> Register (checks 5 sample patches) -> sample, columns and
// label report shown -> only then Create project or Link.
const hub = { kind: 'dataset', preview: null, entry: null, entryPromise: null, session: 0, busy: false, ready: false }
let hubEntriesCache = null
const HUB_ROLES = [['image', 'Image', true], ['label', 'Label', true], ['file_name', 'File name', false], ['split', 'Split', false]]
const HUB_SPLITS = ['train', 'val', 'test']

function hubMessage(text, isError = false) {
  const element = byId('hub-message')
  element.textContent = text
  element.classList.toggle('text-danger', isError)
}

// Create and Link need a registration whose checks have been shown; nothing runs twice at once.
function hubSyncActions() {
  for (const id of ['hub-lookup', 'hub-register-dataset', 'hub-register-model']) byId(id).disabled = hub.busy
  for (const id of ['hub-create-project', 'hub-link-preview', 'hub-link-apply']) byId(id).disabled = hub.busy || !hub.entry || !hub.ready
}

// Disable the action buttons while a request runs so a double-click cannot register twice.
function hubBusy(on) {
  hub.busy = on
  hubSyncActions()
}

// A new lookup, location, column or option invalidates any registration and link preview.
function hubResetEntry() {
  hub.entry = null
  hub.entryPromise = null
  hub.ready = false
  hub.session += 1
  byId('hub-link-apply').classList.add('d-none')
  byId('hub-check').innerHTML = ''
  hubSyncActions()
}

function openHub(kind) {
  hub.kind = kind
  hub.preview = null
  byId('hub-form').reset()
  hubResetEntry()
  hubBusy(false)
  byId('hub-title').textContent = kind === 'dataset' ? 'Dataset from Hugging Face' : 'Model from Hugging Face'
  byId('hub-preview').innerHTML = ''
  byId('hub-columns').innerHTML = ''
  byId('hub-dataset-fields').classList.add('d-none')
  byId('hub-model-fields').classList.add('d-none')
  hubMessage('')
  byId('project-dialog').close()
  byId('hub-dialog').showModal()
}

async function hubLookup() {
  hubResetEntry()
  hubMessage('Looking up…')
  hubBusy(true)
  try {
    const preview = await request('/api/curation/hub/lookup', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ref: byId('hub-ref').value.trim(), kind: hub.kind }) })
    hub.preview = preview
    renderHubPreview(preview)
    hubMessage('')
  } catch (error) { hubMessage(error.message, true) } finally { hubBusy(false) }
}

function renderHubColumns(preview) {
  const names = [...new Set([...Object.keys(preview.columns || {}), ...Object.values(preview.column_map || {}).filter(Boolean)])]
  byId('hub-columns').innerHTML = HUB_ROLES.map(([role, title, required]) => {
    const chosen = preview.column_map?.[role] || ''
    const options = (required ? '' : '<option value="">—</option>') + names.map(name =>
      `<option value="${escapeHtml(name)}" ${name === chosen ? 'selected' : ''}>${escapeHtml(name)}</option>`).join('')
    return `<label class="me-3 small">${title} <select class="form-select form-select-sm d-inline-block w-auto" data-hub-column="${role}">${options}</select></label>`
  }).join('')
  byId('hub-columns').querySelectorAll('[data-hub-column]').forEach(select => select.addEventListener('change', hubResetEntry))
}

function hubColumnMap() {
  return Object.fromEntries([...document.querySelectorAll('[data-hub-column]')].map(select => [select.dataset.hubColumn, select.value || null]))
}

function renderHubPreview(preview) {
  const facts = [
    `<a href="${escapeHtml(preview.url)}" target="_blank" rel="noreferrer"><code>${escapeHtml(preview.repo_id)}@${escapeHtml(preview.revision.slice(0, 8))}</code></a>`,
    preview.task ? escapeHtml(preview.task) : null, preview.license ? `license ${escapeHtml(preview.license)}` : null,
    preview.matches_t3_baseline ? '<span class="badge text-bg-info">T3 v0 baseline commit</span>' : null,
    preview.existing_entry_id ? '<span class="badge text-bg-secondary">already registered</span>' : null,
  ].filter(Boolean)
  const warning = preview.runnable ? '' : `<p class="text-warning small mb-0"><i class="bi bi-exclamation-triangle"></i> ${escapeHtml(preview.not_runnable_reason)}</p>`
  byId('hub-preview').innerHTML = `<p class="mb-1">${facts.join(' · ')}</p>${warning}`
  if (preview.kind === 'dataset') {
    // A column the card did not let us detect can still be chosen by hand.
    const choosable = preview.runnable || (Object.keys(preview.parquet || {}).length && Object.keys(preview.columns || {}).length)
    byId('hub-dataset-fields').classList.toggle('d-none', !choosable)
    renderHubColumns(preview)
    byId('hub-splits').innerHTML = Object.entries(preview.splits).map(([split, count]) =>
      `<label class="me-3"><input type="checkbox" name="hub-split" value="${escapeHtml(split)}" checked> ${escapeHtml(split)}${count == null ? '' : ` (${Number(count).toLocaleString()})`}${HUB_SPLITS.includes(split) ? '' : ' <small class="text-muted">imported unassigned; publishing gives each a split</small>'}</label>`).join('')
    byId('hub-project-name').value = preview.repo_id.split('/')[1]
  } else {
    byId('hub-model-fields').classList.remove('d-none')
    byId('hub-weight-file').innerHTML = preview.weight_files.map(file => `<option>${escapeHtml(file)}</option>`).join('')
    byId('hub-weight-file').disabled = !preview.runnable
    byId('hub-model-trusted').disabled = !preview.runnable
    byId('hub-model-name').value = preview.repo_id.split('/')[1]
    byId('hub-register-model').textContent = preview.runnable ? 'Register model' : 'Add to catalog only'
  }
}

function hubLocation() {
  const mode = document.querySelector('input[name="hub-location"]:checked').value
  return mode === 'custom' ? { mode, root: byId('hub-root').value.trim() } : { mode }
}

// Caches the in-flight promise, so concurrent callers share one registration.
function hubRegisterDataset() {
  if (hub.entryPromise) return hub.entryPromise
  hubMessage('Registering and checking 5 sample patches…')
  const promise = request('/api/curation/hub/datasets', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ref: byId('hub-ref').value.trim(), kind: 'dataset', column_map: hubColumnMap(), location: hubLocation() }) }).then(entry => {
    if (hub.entryPromise !== promise) return entry
    hub.entry = entry
    hubEntriesCache = null
    hubMessage('')
    return entry
  }, error => {
    if (hub.entryPromise === promise) hub.entryPromise = null
    throw error
  })
  hub.entryPromise = promise
  return promise
}

// The label report reads the whole dataset; a stale answer (other entry, mode or session) is dropped.
async function hubLoadLabels() {
  const entry = hub.entry
  if (!entry) return
  const session = hub.session
  const mode = byId('hub-project-mode').value
  const target = byId('hub-labels')
  hub.ready = false
  hubSyncActions()
  target.textContent = `Checking the dataset's labels against ${mode}…`
  let text
  try {
    const rows = await request(`/api/curation/hub/datasets/${entry.id}/labels?dataset_mode=${encodeURIComponent(mode)}`)
    const unknown = rows.filter(row => !row.known)
    text = unknown.length
      ? `${unknown.length} of ${rows.length} labels are not in the ${escapeHtml(mode)} tier:<ul class="mb-0">${unknown.slice(0, 50).map(row =>
        `<li><code>${escapeHtml(row.label)}</code> (${row.count.toLocaleString()})</li>`).join('')}${unknown.length > 50 ? `<li>…and ${unknown.length - 50} more</li>` : ''}</ul>`
      : `All ${rows.length} labels are in the ${escapeHtml(mode)} tier.`
  } catch (error) {
    text = `The label report failed: ${escapeHtml(error.message)}`
  }
  if (session !== hub.session || hub.entry !== entry || byId('hub-project-mode').value !== mode) return
  target.innerHTML = text
  hub.ready = true
  hubSyncActions()
}

async function hubRegister() {
  hubBusy(true)
  try {
    const entry = await hubRegisterDataset()
    if (hub.entry !== entry) return
    const sample = entry.sample || {}
    byId('hub-check').innerHTML = `<div><b>${sample.found ?? '?'} of ${sample.checked ?? '?'}</b> sample patches found.</div><div id="hub-labels"></div>`
    hubBusy(false)
    await hubLoadLabels()
  } catch (error) { hubMessage(error.message, true) } finally { hubBusy(false) }
}

async function hubCreateProject() {
  if (typeof confirmDiscardPending === 'function' && !(await confirmDiscardPending())) return  // the new project replaces the source workspace
  const entry = hub.entry
  if (!entry) { hubMessage('Register the dataset first', true); return }
  const splits = [...document.querySelectorAll('input[name="hub-split"]:checked')].map(input => input.value)
  if (!splits.length) { hubMessage('Choose at least one split', true); return }
  hubBusy(true)
  try {
    const created = await request(`/api/curation/hub/datasets/${entry.id}/projects`, { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: byId('hub-project-name').value.trim(), dataset_mode: byId('hub-project-mode').value, splits }) })
    byId('hub-dialog').close()
    state.projectId = created.project.id
    await loadProjects()
  } catch (error) { hubMessage(error.message, true) } finally { hubBusy(false) }
}

// Polls the import until it finishes. Gives up if the dialog closes or the project changes,
// and tolerates up to 3 consecutive failed polls.
async function hubWaitFor(projectId, operationId) {
  const session = hub.session
  let failures = 0
  for (;;) {
    if (!byId('hub-dialog').open || state.projectId !== projectId || hub.session !== session) throw new Error('Link preview cancelled')
    let operation
    try {
      operation = await request(`/api/curation/projects/${projectId}/imports/${operationId}`)
      failures = 0
    } catch (error) {
      if (++failures > 3) throw error
      await new Promise(resolve => setTimeout(resolve, 1500))
      continue
    }
    if (!['queued', 'starting', 'running'].includes(operation.status)) return operation
    hubMessage(`Reading the dataset… ${operation.processed_rows.toLocaleString()} rows`)
    await new Promise(resolve => setTimeout(resolve, 1500))
  }
}

async function hubLink(dryRun) {
  if (!state.projectId) { hubMessage('Choose a project first', true); return }
  if (!dryRun && typeof confirmDiscardPending === 'function' && !(await confirmDiscardPending())) return  // applying reloads the project
  const entry = hub.entry
  if (!entry) { hubMessage('Register the dataset first', true); return }
  hubBusy(true)
  try {
    const operation = await request(`/api/curation/hub/datasets/${entry.id}/links`, { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: state.projectId, adopt_splits: byId('hub-adopt-splits').checked, dry_run: dryRun }) })
    const done = await hubWaitFor(state.projectId, operation.id)
    if (done.status !== 'completed') { hubMessage(done.error_message || `Link ${done.status}`, true); return }
    const r = done.sources[0].result
    const summary = `${r.matched.toLocaleString()} matched · ${r.unmatched.toLocaleString()} not in this project · ${r.ambiguous} ambiguous · ${r.split_changes.toLocaleString()} split changes · ${r.kept_csv_split.toLocaleString()} CSV splits kept · ${r.already_linked.toLocaleString()} already linked`
    if (dryRun) {
      hubMessage(`Preview: ${summary}. Linking adds no items. Published versions keep their frozen splits; only the next version changes.`)
      // Applying rewrites splits on a project others curate: leads only (the server agrees).
      if (canDo('project-lead')) byId('hub-link-apply').classList.remove('d-none')
      else hubMessage(`Preview: ${summary}. Ask a lead to apply this link.`)
    } else {
      hubMessage(`Linked: ${summary}.`)
      byId('hub-link-apply').classList.add('d-none')
      await loadProjects()
    }
  } catch (error) { hubMessage(error.message, true) } finally { hubBusy(false) }
}

// "Resume import" in the import panel (curation.js shows it): the project's dataset, same splits.
async function resumeHubImport() {
  const project = currentProject()
  const entryId = project?.source_config?.hub?.entry_id
  if (!project || !entryId) return
  byId('resume-import').disabled = true
  try {
    const operation = await request(`/api/curation/hub/datasets/${entryId}/projects/${project.id}/imports`, { method: 'POST' })
    renderImport(operation)
  } catch (error) {
    showNotice(error.message)
    byId('resume-import').disabled = false
  }
}

const hubWatched = new Set()
let hubWatchTimer = null
let hubWatchSignature = ''
let hubWatchChangedAt = Date.now()
const HUB_WATCH_IDLE_MS = 30 * 60 * 1000

// Registrations the current person made (another tab or an earlier visit): the
// registry keeps the name they registered under.
function hubIsMine(entry) {
  const user = session.user
  return !!user && !!entry.created_by_name && [user.label, user.display_name, user.username].includes(entry.created_by_name)
}

// Start (or restart) watching: a fresh registration resets the idle clock.
function hubWatch(id) {
  hubWatched.add(id)
  hubWatchChangedAt = Date.now()
  watchRegistrations()
}

async function hubRegisterModel() {
  hubBusy(true)
  let entry
  try {
    entry = await request('/api/curation/hub/models', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ref: byId('hub-ref').value.trim(), kind: 'model', weight_file: hub.preview?.runnable ? byId('hub-weight-file').value : null,
        name: byId('hub-model-name').value.trim() || null, trusted: byId('hub-model-trusted').checked }) })
  } catch (error) { hubMessage(error.message, true); hubBusy(false); return }
  hubEntriesCache = null
  byId('hub-dialog').close()
  hubBusy(false)
  if (['queued', 'downloading'].includes(entry.status)) {
    showNotice(`Downloading ${entry.repo_id} in the background. It joins the model list when it is ready and is validated automatically when the worker is online.`)
    hubWatch(entry.id)
  } else if (entry.status === 'failed') {
    showNotice(`${entry.repo_id} is already registered but failed: ${entry.error_message || 'unknown error'}. Retry it from the Registry panel.`)
  } else if (entry.status === 'ready') {
    showNotice(`Already registered: ${entry.repo_id} is in the model list.`)
    if (state.projectId) loadEvaluationCatalog().catch(error => showNotice(error.message))
  } else if (state.projectId) {
    loadEvaluationCatalog().catch(error => showNotice(error.message))
  }
}

// While a model registration is queued or downloading, check every 3 s and
// say when one this person registered finishes or fails. Polling stops after
// 30 minutes without a change (a coordinator that is off would never move them).
async function watchRegistrations() {
  clearTimeout(hubWatchTimer)
  let entries
  try { entries = await request('/api/curation/hub/entries?kind=model') } catch (_) { if (hubWatched.size) hubWatchTimer = setTimeout(watchRegistrations, 10000); return }
  const pending = new Set(entries.filter(entry => ['queued', 'downloading'].includes(entry.status)).map(entry => entry.id))
  let finished = false
  for (const entry of entries) {
    if (!hubWatched.has(entry.id) || pending.has(entry.id)) continue
    hubWatched.delete(entry.id)
    finished = true
    if (entry.status === 'failed') showNotice(`Registering ${entry.repo_id} failed: ${entry.error_message || 'unknown error'}. Retry it from the Registry panel.`)
    else showNotice(`${entry.repo_id} is registered${entry.classifier_id ? ' and in the model list' : ''}.`)
  }
  entries.filter(entry => pending.has(entry.id) && hubIsMine(entry)).forEach(entry => hubWatched.add(entry.id))
  const signature = entries.filter(entry => pending.has(entry.id)).map(entry => `${entry.id}:${entry.status}`).join(',')
  if (signature !== hubWatchSignature || finished) { hubWatchSignature = signature; hubWatchChangedAt = Date.now() }
  if (byId('hub-registry-dialog').open) {
    try { renderRegistry(await request('/api/curation/hub/entries')) } catch (_) { /* the next poll retries */ }
  }
  if (finished && state.projectId) loadEvaluationCatalog().catch(error => showNotice(error.message))
  if (pending.size && Date.now() - hubWatchChangedAt < HUB_WATCH_IDLE_MS) hubWatchTimer = setTimeout(watchRegistrations, 3000)
}

const HUB_STATUS = { queued: 'queued — waiting for the coordinator', downloading: 'downloading…', ready: 'ready', failed: 'failed', registered: 'registered' }

function renderRegistry(entries) {
  const list = byId('hub-registry-list')
  list.innerHTML = entries.length ? `<table class="table table-sm"><thead><tr><th>Repository</th><th>Kind</th><th>Task</th><th>Status</th><th>Use</th><th>Registered</th></tr></thead><tbody>${entries.map(entry => `
    <tr><td><a href="${escapeHtml(entry.url)}" target="_blank" rel="noreferrer">${escapeHtml(entry.repo_id)}</a> <code>${escapeHtml(entry.short_revision)}</code>${entry.matches_t3_baseline ? ' <span class="badge">T3 v0</span>' : ''}</td>
    <td>${escapeHtml(entry.kind)}</td><td>${escapeHtml(entry.task || '—')}${entry.runnable ? '' : ` <small class="text-muted">(${escapeHtml(entry.not_runnable_reason || 'catalog only')})</small>`}</td>
    <td>${escapeHtml(HUB_STATUS[entry.status] || entry.status)}${entry.error_message ? `<br><small class="text-danger">${escapeHtml(entry.error_message)}</small>` : ''}${entry.status === 'failed' && entry.kind === 'model' && canDo('create') ? ` <button type="button" class="btn btn-sm btn-outline-primary" data-hub-retry="${entry.id}">Retry</button>` : ''}</td>
    <td>${entry.kind === 'model' ? (entry.classifier_id ? 'classifier registered' : '—') : (entry.projects.map(p => escapeHtml(p.name)).join(', ') || '—')}</td>
    <td>${escapeHtml(entry.created_by_name || '')}<br><small>${new Date(entry.created_at).toLocaleString()}</small></td></tr>`).join('')}</tbody></table>`
    : '<p class="text-muted">Nothing registered yet. Add datasets from "New project" and models from the confusion matrix view.</p>'
  list.querySelectorAll('[data-hub-retry]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true
    try {
      const entry = await request(`/api/curation/hub/entries/${button.dataset.hubRetry}/retry`, { method: 'POST' })
      hubWatch(entry.id)
    } catch (error) { showNotice(error.message); button.disabled = false }
  }))
}

async function openRegistry() {
  hubEntriesCache = null
  byId('hub-registry-dialog').showModal()
  byId('hub-registry-list').textContent = 'Loading…'
  try { renderRegistry(await request('/api/curation/hub/entries')) } catch (error) { byId('hub-registry-list').textContent = error.message }
}

byId('project-from-hub').addEventListener('click', () => openHub('dataset'))
byId('model-from-hub').addEventListener('click', () => openHub('model'))
byId('hub-registry-button').addEventListener('click', openRegistry)
byId('hub-lookup').addEventListener('click', hubLookup)
byId('hub-ref').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); if (!hub.busy) hubLookup() } })
byId('hub-register-dataset').addEventListener('click', hubRegister)
byId('hub-create-project').addEventListener('click', hubCreateProject)
byId('hub-form').addEventListener('submit', event => event.preventDefault())
for (const id of ['hub-ref', 'hub-root']) byId(id).addEventListener('input', hubResetEntry)
document.querySelectorAll('input[name="hub-location"]').forEach(radio => radio.addEventListener('change', hubResetEntry))
byId('hub-adopt-splits').addEventListener('change', () => byId('hub-link-apply').classList.add('d-none'))
// The label report depends on the tier: a new mode re-reads it, the registration stands.
byId('hub-project-mode').addEventListener('change', hubLoadLabels)
byId('resume-import').addEventListener('click', resumeHubImport)
byId('hub-link-preview').addEventListener('click', () => hubLink(true))
byId('hub-link-apply').addEventListener('click', () => hubLink(false))
byId('hub-register-model').addEventListener('click', hubRegisterModel)
document.querySelectorAll('[data-close-dialog]').forEach(button =>
  button.addEventListener('click', () => byId(button.dataset.closeDialog).close()))
byId('evaluation-model').addEventListener('change', async event => {
  const classifierId = event.target.value
  const note = byId('evaluation-hub-note') || byId('evaluation-run-form').insertAdjacentElement('afterend',
    Object.assign(document.createElement('div'), { id: 'evaluation-hub-note', className: 'small text-muted' }))
  note.textContent = ''
  if (!classifierId || !state.projectId) return
  try {
    hubEntriesCache = hubEntriesCache || await request('/api/curation/hub/entries')
    const model = hubEntriesCache.find(entry => entry.kind === 'model' && entry.classifier_id === classifierId)
    const project = await request(`/api/curation/projects/${state.projectId}`)
    const hubRepos = [project.source_config?.hub, ...(project.source_config?.hub_links || [])].filter(Boolean)
    const trainedOn = new Set(model?.trained_on || [])
    const overlap = hubRepos.find(link => trainedOn.has(link.repo_id))
    if (overlap) {
      document.querySelectorAll('input[name="evaluation-splits"]').forEach(box => { box.checked = box.value !== 'train' })
      note.textContent = `This model was trained on ${overlap.repo_id}; scoring its training split only measures memorisation, so val and test are selected.`
      const entryId = overlap.entry_id
      const labels = await request(`/api/curation/hub/datasets/${entryId}/labels?dataset_mode=${encodeURIComponent(project.dataset_mode)}&classifier_id=${classifierId}`)
      const unknown = labels.filter(row => row.in_model === false)
      if (unknown.length) note.textContent += ` ${unknown.length} label(s) are not classes of this model (${unknown.slice(0, 5).map(row => row.effective).join(', ')}…); the run will be refused until they are mapped or excluded.`
    }
  } catch (error) { note.textContent = error.message }
})

// Anything still downloading (for example after a page reload) keeps being watched.
watchRegistrations()

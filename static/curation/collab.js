// Collaboration: project members, change sets and history, comparing people,
// combined releases and project comparison. Classic script loaded after
// curation.js and hub.js; uses their globals (byId, request, state, session,
// canDo, escapeHtml, showNotice, currentProject, timeAgo, fmt).

// ── Members ──────────────────────────────────────────────────────────────────
let membersCache = { projectId: null, list: [] }

async function projectMembers(force = false) {
  if (!state.projectId) return []
  if (force || membersCache.projectId !== state.projectId) {
    membersCache = { projectId: state.projectId, list: await request(`/api/curation/projects/${state.projectId}/members`) }
  }
  return membersCache.list
}

async function renderMembership() {
  const notice = byId('membership-notice')
  const project = currentProject()
  if (!project || !session.user || canDo('project-curator')) { notice.classList.add('d-none'); return }
  const projectId = state.projectId
  notice.classList.add('d-none')
  let leads = []
  try { leads = (await projectMembers()).filter(m => m.role === 'lead').map(m => m.display_name || m.username) } catch (_) {}
  if (state.projectId !== projectId || canDo('project-curator')) return
  notice.textContent = `You can view this project. To curate it, ask a project lead${leads.length ? ` (${leads.join(', ')})` : ''} to add you.`
  notice.classList.remove('d-none')
}

async function openMembers() {
  if (!state.projectId) return
  byId('members-dialog').showModal()
  byId('members-message').textContent = ''
  byId('members-subtitle').textContent = currentProject()?.name || ''
  await renderMembersList(true)
}

async function renderMembersList(force) {
  const list = byId('members-list')
  try {
    const members = await projectMembers(force)
    const lead = canDo('project-lead')
    list.innerHTML = `<table class="table table-sm mb-0"><thead><tr><th>Name</th><th>Role</th><th>Added</th><th></th></tr></thead><tbody>${members.map(m => `
      <tr><td>${escapeHtml(m.display_name || m.username)} <small class="text-muted">${escapeHtml(m.username)}</small></td>
      <td>${lead ? `<select class="form-select form-select-sm" data-member-role="${m.user_id}"><option value="curator" ${m.role === 'curator' ? 'selected' : ''}>Curator</option><option value="lead" ${m.role === 'lead' ? 'selected' : ''}>Lead</option></select>` : escapeHtml(m.role)}</td>
      <td><small>${escapeHtml(m.added_by_name || '')} ${escapeHtml(timeAgo(m.added_at))}</small></td>
      <td>${lead ? `<button type="button" class="btn btn-sm btn-outline-danger" data-member-remove="${m.user_id}">Remove</button>` : ''}</td></tr>`).join('')}</tbody></table>`
    list.querySelectorAll('[data-member-role]').forEach(select => select.addEventListener('change', () =>
      memberAction(`/members/${select.dataset.memberRole}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ role: select.value }) })))
    list.querySelectorAll('[data-member-remove]').forEach(button => button.addEventListener('click', async () => {
      const ok = await window.OF.ui.confirm({
        title: 'Remove this member from the project?', message: 'Their past changes stay in the history.',
        confirmLabel: 'Remove member', danger: true,
      })
      if (ok) memberAction(`/members/${button.dataset.memberRemove}`, { method: 'DELETE' })
    }))
  } catch (error) { list.textContent = error.message }
}

async function memberAction(path, options) {
  const message = byId('members-message')
  try {
    await request(`/api/curation/projects/${state.projectId}${path}`, options)
    message.textContent = 'Saved.'
    message.className = 'small mt-2 mb-0 text-success'
    await refreshMyRole()
    await renderMembersList(true)
    return true
  } catch (error) {
    message.textContent = error.message
    message.className = 'small mt-2 mb-0 text-danger'
    await renderMembersList(true)
    return false
  }
}

async function refreshMyRole() {
  const fresh = await request(`/api/curation/projects/${state.projectId}`)
  const project = currentProject()
  if (project) project.my_role = fresh.my_role
  applyRoleGates()
  renderMembership()
}

byId('members-button').addEventListener('click', openMembers)
byId('members-add').addEventListener('submit', event => {
  event.preventDefault()
  const username = byId('members-username').value.trim()
  if (!username) return
  memberAction('/members', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, role: byId('members-role').value }) })
    .then(ok => { if (ok) byId('members-username').value = '' })
})
byId('members-username').addEventListener('input', async event => {
  const query = event.target.value.trim()
  if (query.length < 2) return
  try {
    const users = await request(`/api/curation/users?query=${encodeURIComponent(query)}`)
    byId('members-user-options').innerHTML = users.map(u => `<option value="${escapeHtml(u.username)}">${escapeHtml(u.display_name || '')}</option>`).join('')
  } catch (_) { /* the form still accepts a typed username */ }
})

// ── Change sets ──────────────────────────────────────────────────────────────
const KIND_NAMES = { relabel: 'relabels', inclusion: 'inclusion changes', quality: 'quality decisions', point: 'points added', review: 'review changes', added: 'items added', other: 'other' }
const describeKinds = kinds => Object.entries(kinds || {}).map(([kind, n]) => `${fmt(n)} ${KIND_NAMES[kind] || kind}`).join(' · ')
const describeClasses = classes => (classes || []).map(c => `${escapeHtml(c.label)} ${fmt(c.items)}`).join(' · ')
let commitTarget = null   // null = me; a user id when a lead commits for someone

async function refreshCommitPill() {
  const pill = byId('commit-pill')
  if (!state.projectId || !canDo('project-curator')) { pill.classList.add('d-none'); return }
  try {
    const mine = await request(`/api/curation/projects/${state.projectId}/change-sets/pending?user=me`)
    pill.textContent = `${fmt(mine.count)} uncommitted change${mine.count === 1 ? '' : 's'} · Commit…`
    pill.classList.toggle('d-none', !mine.count)
  } catch (_) { pill.classList.add('d-none') }
}

async function openCommit(userId = null, name = null) {
  commitTarget = userId
  const pending = await request(`/api/curation/projects/${state.projectId}/change-sets/pending?user=${userId ?? 'me'}`)
  byId('commit-dialog').querySelector('strong').textContent = name ? `Commit ${name}'s changes` : 'Commit your changes'
  byId('commit-summary').innerHTML = `<b>${fmt(pending.count)} changes</b>: ${escapeHtml(describeKinds(pending.summary.kinds))}<br>Classes: ${describeClasses(pending.summary.classes) || '—'}`
  byId('commit-recent').innerHTML = pending.recent.map(e => `<div>${escapeHtml(e.event_type.replaceAll('_', ' '))} · item ${e.item_id ?? '—'}${e.previous_value?.current_label && e.new_value?.current_label && e.previous_value.current_label !== e.new_value.current_label ? ` · ${escapeHtml(e.previous_value.current_label)} → ${escapeHtml(e.new_value.current_label)}` : ''} · ${escapeHtml(timeAgo(e.created_at))}</div>`).join('')
  byId('commit-message').value = ''
  byId('commit-error').textContent = ''
  updateCommitButton()
  byId('commit-dialog').showModal()
}

function updateCommitButton() {
  const length = byId('commit-message').value.trim().length
  byId('commit-counter').textContent = `${length}/500`
  byId('commit-confirm').disabled = length < 1 || length > 500
}

async function confirmCommit() {
  byId('commit-confirm').disabled = true
  try {
    const body = { message: byId('commit-message').value.trim() }
    if (commitTarget !== null) body.user_id = commitTarget
    const done = await request(`/api/curation/projects/${state.projectId}/change-sets`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    byId('commit-dialog').close()
    showNotice(`Committed ${fmt(done.change_count)} changes: “${done.message}”`)
    refreshCommitPill()
    if (state.view === 'versions') loadHistory()
  } catch (error) {
    byId('commit-error').textContent = error.message
    updateCommitButton()
  }
}

byId('commit-pill').addEventListener('click', () => openCommit().catch(error => showNotice(error.message)))
byId('commit-message').addEventListener('input', updateCommitButton)
byId('commit-confirm').addEventListener('click', confirmCommit)

// ── History ──────────────────────────────────────────────────────────────────
let historyRequest = 0
let historyAuthors = { projectId: null, people: new Map() }   // unfiltered authors, per project
const historyDetails = new Map()                              // change-set id -> loaded detail

async function loadHistory() {
  if (!state.projectId) return
  const projectId = state.projectId
  const requestId = ++historyRequest
  const params = new URLSearchParams()
  if (byId('history-person').value) params.set('user_id', byId('history-person').value)
  if (byId('history-since').value) params.set('since_version', byId('history-since').value)
  const base = `/api/curation/projects/${projectId}/change-sets`
  if (historyAuthors.projectId !== projectId) { historyAuthors = { projectId, people: new Map() }; historyDetails.clear() }
  const needAuthors = params.toString() !== '' && !historyAuthors.people.size
  const [sets, open, versionList, members, allSets] = await Promise.all([
    request(`${base}?${params}`), request(`${base}/uncommitted`), request(`/api/curation/projects/${projectId}/versions`),
    projectMembers().catch(() => []), needAuthors ? request(base) : null])
  if (projectId !== state.projectId || requestId !== historyRequest) return
  const authors = historyAuthors.people
  members.forEach(m => authors.set(String(m.user_id), m.display_name || m.username))
  ;(allSets || (params.toString() ? [] : sets)).forEach(cs => cs.author_user_id && authors.set(String(cs.author_user_id), cs.author_name))
  sets.forEach(cs => cs.author_user_id && authors.set(String(cs.author_user_id), cs.author_name))
  open.forEach(u => authors.set(String(u.user_id), u.name))
  fillHistoryFilters(authors, versionList.versions)
  const openIds = [...document.querySelectorAll('[data-history-detail]')].filter(box => !box.classList.contains('d-none')).map(box => box.dataset.historyDetail)
  const lead = canDo('project-lead')
  byId('history-uncommitted').innerHTML = open.length
    ? `Uncommitted: ${open.map(u => `${escapeHtml(u.name)} ${fmt(u.count)}${lead ? ` <button type="button" class="btn btn-link btn-sm p-0" data-commit-for="${u.user_id}" data-commit-name="${escapeHtml(u.name)}">Commit for ${escapeHtml(u.name)}</button>` : ''}`).join(' · ')}`
    : 'Nothing uncommitted.'
  byId('history-list').innerHTML = sets.length ? sets.map(cs => `<article class="history-item">
      <header><b>${escapeHtml(cs.author_name)}</b> · ${escapeHtml(cs.message)} <span class="version-meta">${fmt(cs.change_count)} changes · ${escapeHtml(timeAgo(cs.created_at))}${cs.committed_by_name ? ` · committed by ${escapeHtml(cs.committed_by_name)}` : ''}</span></header>
      <div class="small">${escapeHtml(describeKinds(cs.summary.kinds))}${cs.summary.classes?.length ? ` · ${describeClasses(cs.summary.classes)}` : ''}</div>
      <button type="button" class="btn btn-sm btn-outline-secondary mt-1" data-history-open="${cs.id}">Changes</button>
      <div class="history-detail d-none" data-history-detail="${cs.id}"></div></article>`).join('')
    : '<div class="empty-state"><i class="bi bi-clock-history" aria-hidden="true"></i><strong>No history yet</strong><p>Edit, then commit with a message.</p></div>'
  document.querySelectorAll('[data-commit-for]').forEach(b => b.addEventListener('click', () => openCommit(Number(b.dataset.commitFor), b.dataset.commitName).catch(error => showNotice(error.message))))
  document.querySelectorAll('[data-history-open]').forEach(b => b.addEventListener('click', () => toggleHistoryDetail(b.dataset.historyOpen)))
  openIds.forEach(id => { if (document.querySelector(`[data-history-detail="${id}"]`)) toggleHistoryDetail(id) })   // keep expanded entries open
}

function fillHistoryFilters(people, versions) {
  const person = byId('history-person')
  const keepPerson = person.value
  person.innerHTML = '<option value="">Everyone</option>' + [...people].map(([id, name]) => `<option value="${id}">${escapeHtml(name)}</option>`).join('')
  person.value = people.has(keepPerson) ? keepPerson : ''
  const since = byId('history-since')
  const keepSince = since.value
  const published = versions.filter(v => v.status === 'published').map(v => String(v.version))
  since.innerHTML = '<option value="">All time</option>' + published.map(n => `<option value="${n}">Since v${n}</option>`).join('')
  since.value = published.includes(keepSince) ? keepSince : ''
}

async function toggleHistoryDetail(id) {
  const box = document.querySelector(`[data-history-detail="${id}"]`)
  const opening = box.classList.contains('d-none')
  box.classList.toggle('d-none', !opening)
  if (!opening || box.dataset.loaded) return
  try {
    if (!historyDetails.has(id)) historyDetails.set(id, await request(`/api/curation/projects/${state.projectId}/change-sets/${id}`))
    const detail = historyDetails.get(id)
    box.innerHTML = `<button type="button" class="btn btn-sm btn-primary my-1" data-history-show="${id}">Show these ${fmt(detail.item_ids.length)} items</button>` +
      detail.events.map(e => `<div class="small">${escapeHtml(e.event_type.replaceAll('_', ' '))} · item ${e.item_id ?? '—'}${['current_label', 'included', 'review_state'].map(field => e.previous_value && field in e.previous_value && e.previous_value[field] !== e.new_value?.[field] ? ` · ${field.replace('_', ' ')} ${escapeHtml(String(e.previous_value[field]))} → ${escapeHtml(String(e.new_value?.[field]))}` : '').join('')}${e.reason ? ` · “${escapeHtml(e.reason)}”` : ''}</div>`).join('')
    box.dataset.loaded = '1'
    box.querySelector('[data-history-show]').addEventListener('click', () => showItems(detail.item_ids))
  } catch (error) { box.textContent = error.message }
}

// Open the crop grid on exactly these items. Same mechanism as the matrix drill-down:
// currentFilters() sends item_ids when state.matrixFilter.evaluation is set, taking the ids
// from state.matrixLinkedItemIds. Every other filter is cleared first so nothing hides them.
// The ids travel in a GET query string, so the list is capped.
const SHOW_ITEMS_CAP = 1500
async function showItems(itemIds) {
  if (!itemIds.length) return
  // Settle unsaved edits before the filters change, so a refusal leaves the source view exactly as it was.
  if (state.view === 'source' && typeof confirmDiscardPending === 'function' && !(await confirmDiscardPending())) return
  state.selectedTaxonomy.clear()
  state.selectedCodes.clear()
  ;['filter-image', 'filter-label', 'taxonomy-filter', 'filter-state', 'filter-included', 'quality-filter', 'filter-region', 'filter-island', 'filter-year', 'filter-site', 'filter-annotator', 'filter-origin']
    .forEach(id => { if (byId(id)) byId(id).value = '' })
  renderFilterChips()
  state.matrixLinkedItemIds = itemIds.slice(0, SHOW_ITEMS_CAP)
  state.matrixFilter = { actualAxis: 'source', evaluation: true, label: `Change set · ${fmt(state.matrixLinkedItemIds.length)} items${itemIds.length > SHOW_ITEMS_CAP ? ` (first ${fmt(SHOW_ITEMS_CAP)} of ${fmt(itemIds.length)})` : ''}` }
  renderMatrixDrilldown()
  setView('crop', makeBackTarget('versions'))
}

byId('history-person').addEventListener('change', () => loadHistory().catch(error => showNotice(error.message)))
byId('history-since').addEventListener('change', () => loadHistory().catch(error => showNotice(error.message)))

// ── Compare people ───────────────────────────────────────────────────────────
async function fillPeople() {
  if (!state.projectId) return
  const [open, sets, versionList, members] = await Promise.all([
    request(`/api/curation/projects/${state.projectId}/change-sets/uncommitted`),
    request(`/api/curation/projects/${state.projectId}/change-sets?limit=200`),
    request(`/api/curation/projects/${state.projectId}/versions`), projectMembers(true)])
  const people = new Map(members.map(m => [String(m.user_id), m.display_name || m.username]))
  open.forEach(u => people.set(String(u.user_id), u.name))
  sets.forEach(cs => cs.author_user_id && people.set(String(cs.author_user_id), cs.author_name))
  const options = [...people].map(([id, name]) => `<option value="${id}">${escapeHtml(name)}</option>`).join('')
  for (const [id, index] of [['people-a', 0], ['people-b', 1]]) {
    const select = byId(id)
    const keep = select.value
    select.innerHTML = options
    select.value = people.has(keep) ? keep : ([...people.keys()][index] || '')
  }
  byId('people-since').innerHTML = '<option value="">All time</option>' + versionList.versions.filter(v => v.status === 'published').map(v => `<option value="${v.version}">Since v${v.version}</option>`).join('')
}

async function runPeopleCompare() {
  const a = byId('people-a').value
  const b = byId('people-b').value
  const box = byId('people-result')
  if (!a || !b || a === b) { box.innerHTML = '<span class="small text-muted">Choose two different people.</span>'; return }
  const since = byId('people-since').value
  try {
    const result = await request(`/api/curation/projects/${state.projectId}/people/compare?a=${a}&b=${b}${since ? `&since=${since}` : ''}`)
    const column = p => `<div class="col"><b>${escapeHtml(p.name)}</b><div class="small">${fmt(p.changes)} changes · ${escapeHtml(describeKinds(p.kinds)) || '—'}</div>
      <div class="small">Classes: ${describeClasses(p.classes) || '—'}</div>
      <div class="small">${p.change_sets.map(cs => `• ${escapeHtml(cs.message)} (${fmt(cs.change_count)})`).join('<br>') || 'No change sets'}</div></div>`
    const d = result.disagreements
    box.innerHTML = `<div class="row g-3">${column(result.people[0])}${column(result.people[1])}</div>
      <p class="small mt-2 mb-1">Both changed <b>${fmt(result.overlap)}</b> items · <b>${fmt(d.count)}</b> disagreements${d.count ? ` <button type="button" class="btn btn-sm btn-outline-primary ms-2" id="people-show">Show these items</button>` : ''}</p>
      ${d.items.length ? `<div class="table-responsive"><table class="table table-sm"><thead><tr><th>Item</th><th>Field</th><th>${escapeHtml(result.people[0].name)}</th><th>${escapeHtml(result.people[1].name)}</th><th>Now</th></tr></thead><tbody>${d.items.slice(0, 100).map(r => `<tr><td>${escapeHtml(r.file_name || String(r.item_id))}</td><td>${escapeHtml(r.field.replace('_', ' '))}</td><td>${escapeHtml(String(r.a_value))}</td><td>${escapeHtml(String(r.b_value))}</td><td>${escapeHtml(String(r.current))}</td></tr>`).join('')}</tbody></table></div>` : ''}`
    document.getElementById('people-show')?.addEventListener('click', () => showItems(d.item_ids))
  } catch (error) { box.innerHTML = `<span class="small text-danger">${escapeHtml(error.message)}</span>` }
}

byId('people-run').addEventListener('click', runPeopleCompare)

// ── Releases ─────────────────────────────────────────────────────────────────
let releaseTimer = null
const releaseBreakdown = stats => ({
  by: 'label', splits: ['train', 'val', 'test', 'unassigned'], rare_min: 0,
  rows: Object.entries(stats.classes || {}).map(([key, counts]) => ({ key, counts: { unassigned: 0, ...counts }, total: Object.values(counts).reduce((a, b) => a + b, 0), rare: false })),
  totals: { unassigned: 0, ...(stats.splits || {}) }, total: Object.values(stats.splits || {}).reduce((a, b) => a + b, 0),
})

async function openReleases() {
  byId('releases-dialog').showModal()
  byId('release-detail').innerHTML = ''
  await Promise.all([loadReleases(), fillReleaseSources(), fillProjectCompare()])
}

async function loadReleases() {
  clearTimeout(releaseTimer)
  const list = await request('/api/curation/releases')
  byId('releases-list').innerHTML = list.length ? `<table class="table table-sm"><thead><tr><th>Release</th><th>Mode</th><th>Sources</th><th class="num">Items</th><th>Status</th><th></th></tr></thead><tbody>${list.map(r => `
    <tr><td><b>${escapeHtml(r.name)}</b> r${r.number}</td><td>${escapeHtml(r.dataset_mode)}</td>
    <td class="small">${r.sources.map(s => `${escapeHtml(s.project_name)} v${s.version}`).join(' + ')}</td><td class="num">${fmt(r.item_count)}</td>
    <td>${escapeHtml(r.status)}${r.error_message ? `<br><small class="text-danger">${escapeHtml(r.error_message)}</small>` : ''}</td>
    <td>${r.status === 'published' ? `<button type="button" class="btn btn-sm btn-outline-secondary" data-release-open="${r.id}">Details</button>` : ''}</td></tr>`).join('')}</tbody></table>`
    : '<div class="empty-state"><i class="bi bi-boxes" aria-hidden="true"></i><strong>No releases yet</strong></div>'
  document.querySelectorAll('[data-release-open]').forEach(b => b.addEventListener('click', () => showRelease(list.find(r => String(r.id) === b.dataset.releaseOpen))))
  if (list.some(r => r.status === 'building') && byId('releases-dialog').open) releaseTimer = setTimeout(() => loadReleases().catch(() => {}), 3000)
}

function showRelease(release) {
  const s = release.statistics
  const base = `/api/curation/releases/${release.id}/files`
  byId('release-detail').innerHTML = `<h3 class="h6">${escapeHtml(release.name)} r${release.number}</h3>
    <p class="small mb-1">${fmt(release.item_count)} items · ${fmt(s.duplicates)} duplicates kept once · ${fmt(s.conflicts)} conflicts left out · ${fmt(s.leakage)} source images in more than one split</p>
    <p class="small">${release.files.map(name => `<a href="${base}/${encodeURIComponent(name)}">${escapeHtml(name)}</a>`).join(' · ')}</p><div id="release-breakdown"></div>`
  renderBreakdownTable(document.getElementById('release-breakdown'), releaseBreakdown(s))
}

async function fillReleaseSources() {
  const mode = byId('release-mode').value
  const projects = state.projects.filter(p => p.dataset_mode === mode)
  const rows = await Promise.all(projects.map(async p => {
    const versions = (await request(`/api/curation/projects/${p.id}/versions`)).versions.filter(v => v.status === 'published')
    return `<div class="d-flex gap-2 align-items-center mb-1"><span class="small" style="min-width:16rem">${escapeHtml(p.name)}${p.my_role === 'lead' ? '' : ' <small class="text-muted">(not your project)</small>'}</span>
      <select class="form-select form-select-sm w-auto" data-release-source="${p.id}"><option value="">not included</option>${versions.map(v => `<option value="${v.version}">v${v.version} · ${fmt(v.item_count)} items</option>`).join('')}</select></div>`
  }))
  byId('release-sources').innerHTML = rows.join('') || '<p class="small text-muted">No projects in this mode.</p>'
}

byId('releases-button').addEventListener('click', () => openReleases().catch(error => showNotice(error.message)))
byId('releases-dialog').addEventListener('close', () => clearTimeout(releaseTimer))
byId('release-mode').addEventListener('change', () => fillReleaseSources().catch(error => showNotice(error.message)))
byId('release-form').addEventListener('submit', async event => {
  event.preventDefault()
  const sources = [...document.querySelectorAll('[data-release-source]')].filter(s => s.value).map(s => ({ project_id: s.dataset.releaseSource, version: Number(s.value) }))
  const message = byId('release-message')
  try {
    const release = await request('/api/curation/releases', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: byId('release-name').value.trim(), dataset_mode: byId('release-mode').value, sources, notes: byId('release-notes').value.trim() || null }) })
    message.textContent = `Building ${release.name} r${release.number}…`
    message.className = 'small ms-2 text-success'
    loadReleases().catch(() => {})
  } catch (error) {
    message.textContent = error.message
    message.className = 'small ms-2 text-danger'
  }
})

// ── Compare projects ─────────────────────────────────────────────────────────
async function fillProjectCompare() {
  const options = state.projects.map(p => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join('')
  byId('pc-a').innerHTML = options
  byId('pc-b').innerHTML = options
  if (state.projects[1]) byId('pc-b').value = state.projects[1].id
  await Promise.all([fillCompareVersions('pc-a'), fillCompareVersions('pc-b')])
}

async function fillCompareVersions(id) {
  const versions = (await request(`/api/curation/projects/${byId(id).value}/versions`)).versions.filter(v => v.status === 'published')
  byId(`${id}-version`).innerHTML = '<option value="live">Live</option>' + versions.map(v => `<option value="${v.version}">v${v.version}</option>`).join('')
}

async function runProjectCompare() {
  const box = byId('pc-result')
  const query = `a=${byId('pc-a').value}&a_version=${byId('pc-a-version').value}&b=${byId('pc-b').value}&b_version=${byId('pc-b-version').value}`
  box.innerHTML = '<span class="small text-muted">Comparing…</span>'
  try {
    const r = await request(`/api/curation/compare-projects?${query}`)
    const ld = r.label_disagreements
    box.innerHTML = `<p class="small mb-1"><b>${escapeHtml(r.a.project)}</b> ${escapeHtml(r.a.ref)} ${fmt(r.a.count)} · <b>${escapeHtml(r.b.project)}</b> ${escapeHtml(r.b.ref)} ${fmt(r.b.count)} · matched ${fmt(r.matched)} · only A ${fmt(r.only_a)} · only B ${fmt(r.only_b)}</p>
      <p class="small mb-1">${fmt(ld.count)} label disagreements${ld.a_item_ids.length && r.a.ref === 'live' ? ` <button type="button" class="btn btn-sm btn-outline-primary ms-1" id="pc-show">Show in ${escapeHtml(r.a.project)}</button>` : ''}: ${ld.pairs.slice(0, 15).map(p => `${escapeHtml(p.a_label)} → ${escapeHtml(p.b_label)} ${fmt(p.count)}`).join(' · ') || 'none'}</p>
      <p class="small mb-1">${fmt(r.split_disagreements.count)} split disagreements${r.split_disagreements.pairs.length ? ': ' + r.split_disagreements.pairs.map(p => `${escapeHtml(p.a_split)} → ${escapeHtml(p.b_split)} ${fmt(p.count)}`).join(' · ') : ''}</p>
      <div class="table-responsive"><table class="table table-sm"><thead><tr><th>Class</th><th class="num">A</th><th class="num">B</th><th class="num">B − A</th></tr></thead><tbody>${r.classes.slice(0, 200).map(c => `<tr><td>${escapeHtml(c.label)}</td><td class="num">${fmt(c.a)}</td><td class="num">${fmt(c.b)}</td><td class="num">${c.diff > 0 ? '+' : ''}${fmt(c.diff)}</td></tr>`).join('')}</tbody></table></div>`
    document.getElementById('pc-show')?.addEventListener('click', async () => {
      byId('releases-dialog').close()
      if (String(state.projectId) !== String(r.a.project_id)) {
        byId('project-select').value = r.a.project_id
        byId('project-select').dispatchEvent(new Event('change'))
        // Kept on the current project by the unsaved-edits guard: its items are not the ones to show.
        if (!(await state.projectSwitch)) return
      }
      showItems(ld.a_item_ids)
    })
  } catch (error) { box.innerHTML = `<span class="small text-danger">${escapeHtml(error.message)}</span>` }
}

byId('pc-a').addEventListener('change', () => fillCompareVersions('pc-a').catch(error => showNotice(error.message)))
byId('pc-b').addEventListener('change', () => fillCompareVersions('pc-b').catch(error => showNotice(error.message)))
byId('pc-run').addEventListener('click', runProjectCompare)

// ── Working alongside other curators (curation/claims.py) ──────────────────────
// This tab claims what it has open: the points of the source image on screen, or the undecided
// patches loaded in Quality review. Other curators' queues leave those out ("Hide others' open
// work"). Every LIVE_POLL_MS, while the page is visible, it renews its claims and asks what other
// people changed since it last looked, then drops their finished work from the lists it loaded.
// The image on screen is never dropped: a notice says someone changed it, and its revision still
// guards every save.
const LIVE_POLL_MS = 15000
const LIVE_MAX_SKIPS = 5
const live = { holder: newHolder(), projectId: null, watermark: null, claimKey: '', claimedUris: new Map(), claimedIds: new Map(), polling: false, online: [] }

function newHolder() {
  try { return crypto.randomUUID() } catch (_) { return `tab-${Date.now()}-${Math.random().toString(36).slice(2)}` }
}

const liveUrl = (projectId, path) => `/api/curation/projects/${encodeURIComponent(projectId)}/${path}`

// Called before a project's lists load, so a change landing in between is replayed, never missed.
async function resetChangeFeed() {
  if (live.projectId && live.projectId !== state.projectId && live.claimKey) postClaims(live.projectId, [])
  live.projectId = state.projectId
  live.watermark = null
  live.claimKey = ''
  live.claimedUris = new Map()
  live.claimedIds = new Map()
  if (!state.projectId) return
  try { live.watermark = (await request(liveUrl(state.projectId, 'changes'))).last_id } catch (_) { /* the next poll retries */ }
  sendPresence()  // shows up as online straight away, not at the next poll
}

function postClaims(projectId, ids, options = {}) {
  return request(liveUrl(projectId, 'claims'), {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ holder: live.holder, item_ids: ids }), ...options,
  })
}

function claimIds() {
  if (!canDo('project-curator')) return []
  if (state.view === 'source' && state.currentImage) return state.items.map(item => item.id)
  if (state.view === 'quality' && state.qualityScope === 'patch') {
    return state.qualityItems.filter(item => !item.savedDecision).map(item => item.reviewId).slice(0, 1000)
  }
  return []
}

// Make this tab's claims what it has open now. Returns the server's answer, or null when there was
// nothing to say or it could not be said (claims only steer queues; failing one never blocks work).
async function syncClaims() {
  if (!state.projectId || live.projectId !== state.projectId) return null
  const ids = claimIds()
  if (!ids.length && !live.claimKey) return null
  live.claimKey = ids.join(',')
  let answer
  try { answer = await postClaims(state.projectId, ids) } catch (_) { return null }
  const held = answer.held || []
  const shown = new Set(state.items.map(item => item.id))
  const heldHere = state.view === 'source' ? held.filter(entry => shown.has(entry.item_id)) : []
  state.currentHeldBy = heldHere.length ? [...new Set(heldHere.map(entry => entry.user_name))].join(', ') : null
  return answer
}

// After an image opens: claim it, and say so when someone else already has it.
async function claimOpenedImage() {
  state.currentHeldBy = null
  await syncClaims()
  sendPresence()  // "Reviewing <image>" on others' Activity tab straight away
  if (state.currentHeldBy) showNotice(`${state.currentHeldBy} is reviewing this image right now.`)
  return !state.currentHeldBy
}

// Automatic moves (the first image, Save & next) step past images someone else has open.
async function skipHeldImages(direction = 1) {
  for (let tries = 0; tries < LIVE_MAX_SKIPS && state.currentHeldBy && state.view === 'source'; tries += 1) {
    const at = state.images.findIndex(image => image.first_item_id === state.currentImage?.first_item_id)
    const next = state.images[at + direction]
    if (at < 0 || !next) return
    if (!(await selectImage(next.first_item_id))) return
  }
}

// Whether a changed item still belongs in a list loaded under the current filters.
function stillMatches(change, view) {
  if (change.removed && !byId('filter-show-removed').checked) return false
  const reviewState = byId('filter-state').value
  if (reviewState && change.review_state !== reviewState) return false
  const included = byId('filter-included').value
  if (included && String(change.included) !== included) return false
  if (view === 'quality') {
    const decision = byId('quality-filter').value
    if (decision === 'pending' ? change.quality_decision != null : decision && change.quality_decision !== decision) return false
  }
  return true
}

function applyLiveChanges(feed) {
  live.claimedIds = new Map(feed.claimed.map(entry => [entry.item_id, entry.user_name]))
  live.claimedUris = new Map(feed.claimed.map(entry => [entry.source_image_uri, entry.user_name]))
  if (feed.reload) {
    showNotice('Other curators changed a lot since these lists loaded: refresh them to catch up.')
    return
  }
  const hideClaimed = byId('filter-hide-claimed').checked
  const changed = new Map(feed.items.map(item => [item.id, item]))
  const pendingByUri = new Map(feed.images.map(image => [image.source_image_uri, image.pending]))
  const currentUri = state.currentImage?.source_image_uri
  let dropped = 0

  const keepImage = image => image.source_image_uri === currentUri
    || !((hideClaimed && live.claimedUris.has(image.source_image_uri))
      || (byId('filter-hide-handled').checked && pendingByUri.get(image.source_image_uri) === 0))
  const images = state.images.filter(keepImage)
  if (images.length !== state.images.length) {
    dropped += state.images.length - images.length
    state.images = images
    renderQueue()
  }

  const keepItem = view => item => {
    const change = changed.get(item.id ?? item.reviewId)
    if (hideClaimed && live.claimedIds.has(item.id ?? item.reviewId)) return false
    return !change || stillMatches(change, view)
  }
  const fresh = item => {  // what is still shown takes the newer state, so its next save is not refused
    const change = changed.get(item.id ?? item.reviewId)
    return change ? { ...item, review_state: change.review_state, included: change.included, removed: change.removed,
      quality_decision: change.quality_decision, revision: change.revision, updated_by_name: change.updated_by_name } : item
  }
  const crops = state.crops.filter(keepItem('crop')).map(fresh)
  if (crops.length !== state.crops.length || state.crops.some(item => changed.has(item.id))) {
    dropped += state.crops.length - crops.length
    state.crops = crops
    if (state.view === 'crop') renderCropGrid()
  }
  if (state.qualityScope === 'patch') {
    const quality = state.qualityItems.filter(keepItem('quality')).map(fresh)
    if (quality.length !== state.qualityItems.length || state.qualityItems.some(item => changed.has(item.reviewId))) {
      dropped += state.qualityItems.length - quality.length
      state.qualityItems = quality
      if (state.view === 'quality') {
        renderQualitySummary(state.qualityItems)
        renderQualityList()
        renderQualityPaging('patches')
      }
    }
  }

  const onScreen = feed.items.filter(item => item.source_image_uri === currentUri)
  if (state.view === 'source' && onScreen.length) {
    const names = [...new Set(onScreen.map(item => item.updated_by_name).filter(Boolean))].join(', ') || 'Someone'
    showNotice(`${names} changed points on this image. Open it again to see their edits.`)
  }
  if (dropped && window.OF?.ui?.toast) {
    OF.ui.toast(`${fmt(dropped)} item${dropped === 1 ? '' : 's'} handled or opened by other curators left your list`)
  }
}

// ── Presence and the Activity tab (curation/presence.py) ──
// "Online" is a curation page open and visible; a tab in the background drops off after 45 s.
const VIEW_NAMES = { source: 'Source images', crop: 'Crop grid', quality: 'Quality review', status: 'Dataset status',
  matrix: 'Confusion matrix', versions: 'Versions', activity: 'Activity' }

function presenceDetail() {
  if (state.view === 'source' && state.currentImage) return state.currentImage.image_name
  if (state.view === 'quality') return state.qualityScope === 'patch' ? 'patches' : 'source images'
  if (state.matrixFilter?.actual) return `${state.matrixFilter.actual} → ${state.matrixFilter.predicted || 'all'}`
  return null
}

async function sendPresence() {
  if (!state.projectId) return
  try {
    const answer = await request(liveUrl(state.projectId, 'presence'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ holder: live.holder, view: state.view, detail: presenceDetail() }),
    })
    live.online = answer.online || []
  } catch (_) { return }
  renderPresence()
}

function doingText(tab) {
  const where = VIEW_NAMES[tab.view] || tab.view
  if (tab.view === 'source' && tab.detail) return `Reviewing ${tab.detail}`
  return tab.detail ? `${where} · ${tab.detail}` : where
}

function renderPresence() {
  const people = live.online || []
  const here = people.filter(person => person.tabs.some(tab => tab.project_id === state.projectId))
  const badge = byId('online-count')
  badge.textContent = here.length ? String(here.length) : ''
  badge.classList.toggle('d-none', !here.length)
  badge.title = here.map(person => person.name).join(', ')
  if (state.view !== 'activity') return
  byId('online-summary').textContent = `${fmt(people.length)} ${people.length === 1 ? 'person' : 'people'} online · ${fmt(here.length)} in this project. Updates every 15 seconds.`
  const me = session.user?.id
  byId('online-list').innerHTML = people.length ? people.map(person => {
    const initials = person.name.split(/\s+/).map(part => part[0]).join('').slice(0, 2).toUpperCase()
    const tabs = person.tabs.map(tab => `<li>${escapeHtml(doingText(tab))} <span class="text-muted">in ${tab.project_id === state.projectId ? 'this project' : escapeHtml(tab.project)}</span></li>`).join('')
    const work = [person.open_items ? `${fmt(person.open_items)} item${person.open_items === 1 ? '' : 's'} open` : null,
      person.recent_changes ? `${fmt(person.recent_changes)} change${person.recent_changes === 1 ? '' : 's'} in the last 15 min` : 'no changes in the last 15 min',
      person.last_change_at ? `last ${timeAgo(person.last_change_at)}` : null].filter(Boolean).join(' · ')
    return `<li class="online-person"><span class="online-avatar" aria-hidden="true">${escapeHtml(initials)}</span><div>
      <strong>${escapeHtml(person.name)}</strong>${person.user_id === me ? ' <small class="text-muted">(you)</small>' : ''}
      <ul class="online-tabs">${tabs}</ul><small class="text-muted">${escapeHtml(work)}</small></div></li>`
  }).join('') : '<li class="empty-state">Nobody else has curation open right now.</li>'
}

async function renderActivityFeed() {
  if (!state.projectId) return
  const activity = await request(`/api/curation/projects/${state.projectId}/activity?limit=60`)
  byId('activity-feed').innerHTML = activity.events.length
    ? activity.events.map(event => `<li><b>${escapeHtml(event.curator_name)}</b> ${escapeHtml(EVENT_LABELS[event.event_type] || event.event_type)}${event.item_id ? ` item ${event.item_id}` : ''}${describeChange(event) ? ` <span class="activity-change">${escapeHtml(describeChange(event))}</span>` : ''}${event.reason ? ` <q>${escapeHtml(event.reason)}</q>` : ''} <time>${escapeHtml(timeAgo(event.created_at))}</time></li>`).join('')
    : '<li class="empty-state">Nothing has changed yet.</li>'
}

async function loadActivityView() {
  await Promise.all([sendPresence(), renderActivityFeed()])
}

async function pollLive() {
  if (live.polling || document.visibilityState !== 'visible' || !state.projectId) return
  live.polling = true
  try {
    if (live.projectId !== state.projectId || live.watermark == null) { await resetChangeFeed(); return }
    await Promise.all([syncClaims(), sendPresence()])  // the heartbeats: this tab's claims, and that it is here
    if (state.view === 'activity') renderActivityFeed().catch(() => {})
    const projectId = state.projectId
    const feed = await request(`${liveUrl(projectId, 'changes')}?after_id=${live.watermark}`)
    if (projectId !== state.projectId || live.projectId !== projectId) return
    live.watermark = feed.last_id
    applyLiveChanges(feed)
  } catch (_) {
    // Offline for a moment: the next poll catches up from the same watermark.
  } finally {
    live.polling = false
  }
}

setInterval(pollLive, LIVE_POLL_MS)
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') pollLive() })
// Let go on the way out, so others need not wait for the claims to run out.
window.addEventListener('pagehide', () => {
  if (live.projectId && live.claimKey) postClaims(live.projectId, [], { keepalive: true }).catch(() => {})
  if (live.projectId) {
    fetch(liveUrl(live.projectId, 'presence'), {
      method: 'POST', credentials: 'same-origin', keepalive: true, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ holder: live.holder, leaving: true }),
    }).catch(() => {})
  }
})

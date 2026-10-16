/* OceanFrame UI kit: theme, toasts, sheets, popovers and the command palette.
   A classic script loaded by base.html before page scripts, so module and classic pages share window.OF.ui. */
(function () {
  'use strict'
  const OF = window.OF = window.OF || {}
  const ui = OF.ui = OF.ui || {}

  // ── Theme ──────────────────────────────────────────────────────────────────
  const KEY = 'of-theme'
  const media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null
  const themeListeners = new Set()
  function storedTheme() { try { const v = localStorage.getItem(KEY); return v === 'dark' || v === 'light' ? v : 'system' } catch (_) { return 'system' } }
  function resolveTheme(choice) { return choice === 'system' ? (media && media.matches ? 'light' : 'dark') : choice }
  function applyTheme() {
    const next = resolveTheme(storedTheme())
    document.documentElement.setAttribute('data-theme', next)
    themeListeners.forEach(fn => { try { fn(next) } catch (error) { console.warn(error) } })
  }
  ui.theme = {
    get: storedTheme,
    effective: () => document.documentElement.getAttribute('data-theme') || 'dark',
    set(choice) {
      if (!['dark', 'light', 'system'].includes(choice)) return
      try { localStorage.setItem(KEY, choice) } catch (_) { /* private mode: still applies for this page */ }
      if (choice !== 'system') document.documentElement.setAttribute('data-theme', choice)
      applyTheme()
    },
    onChange(fn) { themeListeners.add(fn); return () => themeListeners.delete(fn) },
    /** Current value of a CSS custom property, for canvases and charts. */
    token(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() },
  }
  if (media) (media.addEventListener ? media.addEventListener('change', () => { if (storedTheme() === 'system') applyTheme() }) : media.addListener(() => { if (storedTheme() === 'system') applyTheme() }))

  // ── Toasts ─────────────────────────────────────────────────────────────────
  function toastHost() {
    let host = document.getElementById('of-toasts')
    if (!host) { host = document.createElement('div'); host.id = 'of-toasts'; host.className = 'toast-host'; host.setAttribute('aria-live', 'polite'); document.body.appendChild(host) }
    return host
  }
  ui.toast = function toast(message, opts = {}) {
    const kind = opts.kind || 'info'
    const el = document.createElement('div')
    el.className = `toast toast-${kind}`
    el.setAttribute('role', kind === 'danger' ? 'alert' : 'status')
    const text = document.createElement('span')
    text.className = 'toast-text'
    text.textContent = String(message)
    el.appendChild(text)
    let timer = null
    function close() {
      clearTimeout(timer)
      el.classList.remove('is-in'); el.classList.add('is-out')
      setTimeout(() => el.remove(), 260)
    }
    if (opts.action && opts.action.label) {
      const action = document.createElement('button')
      action.type = 'button'; action.className = 'btn btn-ghost btn-sm'; action.textContent = opts.action.label
      action.addEventListener('click', () => { close(); try { opts.action.onClick() } catch (error) { console.error(error) } })
      el.appendChild(action)
    }
    const dismiss = document.createElement('button')
    dismiss.type = 'button'; dismiss.className = 'toast-close'; dismiss.setAttribute('aria-label', 'Dismiss'); dismiss.textContent = '×'
    dismiss.addEventListener('click', close)
    el.appendChild(dismiss)
    toastHost().appendChild(el)
    requestAnimationFrame(() => el.classList.add('is-in'))
    const timeout = opts.timeout != null ? opts.timeout : (kind === 'danger' ? 0 : 4000)
    if (timeout) timer = setTimeout(close, timeout)
    return { close }
  }

  // ── Sheets (confirm / prompt) ──────────────────────────────────────────────
  let sheetCount = 0
  function sheet({ title, message = '', confirmLabel = 'OK', cancelLabel = 'Cancel', danger = false, input = null }) {
    return new Promise(resolve => {
      const id = `of-sheet-${++sheetCount}`
      const dialog = document.createElement('dialog')
      dialog.className = 'sheet'
      dialog.setAttribute('aria-labelledby', `${id}-title`)
      const form = document.createElement('form')
      form.method = 'dialog'
      const heading = document.createElement('h2')
      heading.className = 'sheet-title'; heading.id = `${id}-title`; heading.textContent = title
      form.appendChild(heading)
      if (message) { const p = document.createElement('p'); p.className = 'sheet-message'; p.textContent = message; form.appendChild(p) }
      let field = null
      let error = null
      if (input) {
        const label = document.createElement('label')
        label.className = 'form-label'; label.textContent = input.label || ''
        field = document.createElement('input')
        field.type = input.type || 'text'; field.className = 'form-control'; field.value = input.value || ''; field.autocomplete = input.type === 'password' ? 'new-password' : 'off'; field.spellcheck = false
        label.appendChild(field)
        form.appendChild(label)
        error = document.createElement('p')
        error.className = 'sheet-error'; error.hidden = true; error.setAttribute('role', 'alert')
        form.appendChild(error)
      }
      const actions = document.createElement('div')
      actions.className = 'sheet-actions'
      const cancel = document.createElement('button')
      cancel.type = 'button'; cancel.className = 'btn'; cancel.textContent = cancelLabel; cancel.dataset.sheet = 'cancel'
      const ok = document.createElement('button')
      ok.type = 'submit'; ok.className = danger ? 'btn btn-danger' : 'btn btn-primary'; ok.textContent = confirmLabel; ok.dataset.sheet = 'confirm'
      actions.append(cancel, ok)
      form.appendChild(actions)
      dialog.appendChild(form)
      document.body.appendChild(dialog)
      let settled = false
      function finish(value) {
        if (settled) return
        settled = true
        try { dialog.close() } catch (_) { /* already closed */ }
        dialog.remove()
        resolve(value)
      }
      cancel.addEventListener('click', () => finish(input ? null : false))
      dialog.addEventListener('cancel', event => { event.preventDefault(); finish(input ? null : false) })
      form.addEventListener('submit', event => {
        event.preventDefault()
        if (!input) return finish(true)
        const value = field.value
        const problem = typeof input.validate === 'function' ? input.validate(value) : null
        if (problem) { error.textContent = problem; error.hidden = false; field.focus(); field.select(); return }
        finish(value)
      })
      dialog.showModal()
      ;(field || (danger ? cancel : ok)).focus()
    })
  }
  ui.confirm = opts => sheet(opts)
  ui.prompt = ({ label, value, validate, type, ...rest }) => sheet({ ...rest, input: { label, value, validate, type } })

  // ── Popovers and menus ─────────────────────────────────────────────────────
  let open = null
  function focusables(panel) { return [...panel.querySelectorAll('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]')] }
  ui.closePopover = function closePopover() {
    if (!open) return
    const { anchor, panel, onDoc, onKey, onResize, onPanelKey } = open
    open = null
    panel.hidden = true
    panel.classList.remove('popover')
    anchor.setAttribute('aria-expanded', 'false')
    document.removeEventListener('pointerdown', onDoc, true)
    document.removeEventListener('keydown', onKey, true)
    panel.removeEventListener('keydown', onPanelKey)
    window.removeEventListener('resize', onResize)
  }
  // placement: 'below' (the default: below the anchor, flipping above when it does not fit) or
  // 'above' (above the anchor, flipping below when it does not fit). Kept when the window resizes.
  ui.popover = function popover(anchor, panel, { placement = 'below' } = {}) {
    if (open && open.panel === panel) { ui.closePopover(); return null }
    ui.closePopover()
    panel.hidden = false
    panel.classList.add('popover')
    function place() {
      const r = anchor.getBoundingClientRect()
      const w = panel.offsetWidth
      const h = panel.offsetHeight
      const rail = anchor.closest('.rail')
      let left = rail && window.innerWidth > 640 ? r.right + 8 : r.left
      let top = rail && window.innerWidth > 640 ? Math.max(8, r.bottom - h) : r.bottom + 6
      left = Math.max(8, Math.min(left, window.innerWidth - w - 8))
      if (placement === 'above' && !rail) {
        top = r.top - h - 6
        if (top < 8) top = r.bottom + 6 + h <= window.innerHeight - 8 ? r.bottom + 6 : 8
      } else if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6)
      panel.style.left = `${left}px`
      panel.style.top = `${top}px`
    }
    place()
    anchor.setAttribute('aria-expanded', 'true')
    const onDoc = event => { if (!panel.contains(event.target) && !anchor.contains(event.target)) ui.closePopover() }
    const onKey = event => {
      if (event.key === 'Escape') { event.stopPropagation(); ui.closePopover(); anchor.focus(); return }
      if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && !event.target.closest('select, input, textarea')) {
        const items = focusables(panel)
        if (!items.length) return
        const index = items.indexOf(document.activeElement)
        const next = items[(index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]
        next.focus()
        event.preventDefault()
      }
    }
    // Keys typed in a popover stay in it: page handlers (the source editor reads arrows and Delete) never see them.
    // Bubble phase, so handlers inside the panel still run; Ctrl/Cmd shortcuts pass.
    const onPanelKey = event => { if (!event.ctrlKey && !event.metaKey) event.stopPropagation() }
    const onResize = () => place()
    document.addEventListener('pointerdown', onDoc, true)
    document.addEventListener('keydown', onKey, true)
    panel.addEventListener('keydown', onPanelKey)
    window.addEventListener('resize', onResize)
    open = { anchor, panel, onDoc, onKey, onResize, onPanelKey }
    const first = focusables(panel)[0]
    if (first) first.focus({ preventScroll: true })
    return ui.closePopover
  }

  // ── Command palette ────────────────────────────────────────────────────────
  const providers = []
  let results = []
  let cursor = 0
  let queryToken = 0
  function score(label, query) {
    if (!query) return 1
    const text = label.toLowerCase()
    const q = query.toLowerCase().trim()
    if (!q) return 1
    if (text.includes(q)) return 100 - text.indexOf(q)
    let at = 0
    for (const ch of q.replace(/\s+/g, '')) { at = text.indexOf(ch, at); if (at < 0) return 0; at += 1 }
    return 10
  }
  async function gather(query) {
    const token = ++queryToken
    const settled = await Promise.allSettled(providers.map(async provider => {
      const items = await provider.items(query)
      const ranked = (items || [])
        .map(item => ({ item, s: provider.filter === false ? 1 : score(`${item.label} ${item.hint || ''} ${item.keywords || ''}`, query) }))
        .filter(row => row.s > 0)
        .sort((a, b) => b.s - a.s)
        .slice(0, provider.limit || 8)
        .map(row => row.item)
      return { group: provider.group, items: ranked }
    }))
    if (token !== queryToken) return null
    return settled.filter(r => r.status === 'fulfilled' && r.value.items.length).map(r => r.value)
  }
  function renderPalette(groups, query = '') {
    lastRenderedQuery = query
    const list = document.getElementById('of-palette-results')
    if (!list) return
    list.textContent = ''
    results = []
    if (!groups.length) {
      const empty = document.createElement('div'); empty.className = 'palette-empty'; empty.textContent = 'No matches'
      list.appendChild(empty)
      return
    }
    for (const group of groups) {
      const heading = document.createElement('div'); heading.className = 'palette-group'; heading.textContent = group.group
      list.appendChild(heading)
      for (const item of group.items) {
        const index = results.length
        const row = document.createElement('button')
        row.type = 'button'; row.className = 'palette-item'; row.setAttribute('role', 'option')
        if (item.icon) { const i = document.createElement('i'); i.className = `bi ${item.icon}`; i.setAttribute('aria-hidden', 'true'); row.appendChild(i) }
        const label = document.createElement('span'); label.textContent = item.label; row.appendChild(label)
        if (item.hint) { const hint = document.createElement('small'); hint.textContent = item.hint; row.appendChild(hint) }
        row.addEventListener('click', () => runResult(index))
        row.addEventListener('mousemove', () => select(index))
        list.appendChild(row)
        results.push({ item, row })
      }
    }
    select(0)
  }
  function select(index) {
    if (!results.length) return
    cursor = (index + results.length) % results.length
    results.forEach((r, i) => r.row.setAttribute('aria-selected', String(i === cursor)))
    results[cursor].row.scrollIntoView({ block: 'nearest' })
  }
  function runResult(index) {
    const chosen = results[index]
    if (!chosen) return
    ui.palette.close()
    Promise.resolve().then(() => chosen.item.run()).catch(error => {
      console.error(error)
      ui.toast((error && error.message) || "That action failed.", { kind: "danger" })
    })
  }
  let debounce = null
  let lastRenderedQuery = ''
  ui.palette = {
    register(provider) { if (provider && typeof provider.items === 'function') providers.push(provider) },
    async open() {
      if (document.body.classList.contains("of-bare")) return  // sign-in: no app to search yet
      const dialog = document.getElementById('of-palette')
      const input = document.getElementById('of-palette-input')
      if (!dialog || !input || dialog.open) return
      ui.closePopover()
      clearTimeout(debounce); debounce = null; lastRenderedQuery = null
      input.value = ''
      dialog.showModal()
      input.focus()
      const groups = await gather('')
      if (groups) renderPalette(groups, '')
    },
    close() { clearTimeout(debounce); debounce = null; const dialog = document.getElementById('of-palette'); if (dialog && dialog.open) dialog.close() },
  }

  // ── Shell wiring ───────────────────────────────────────────────────────────
  // The signed-in user, or null (sign-in off, or signed out). Shares session.js's single /api/auth/me request with the avatar menu.
  async function signedInUser() {
    try {
      const v = window.OF_ASSET_V ? '?v=' + encodeURIComponent(window.OF_ASSET_V) : ''
      const me = await (await import('/static/auth/session.js' + v)).whoami()
      return me && me.auth_enabled && me.user ? me.user : null
    } catch (_) { return null }
  }
  ui.palette.register({
    group: 'Go to',
    items: async () => {
      const user = await signedInUser()
      return [
      { label: 'Library', icon: 'bi-images', run: () => { location.href = '/' } },
      { label: 'Collections', icon: 'bi-collection', run: () => { location.href = '/collections' } },
      { label: 'Curation', icon: 'bi-bullseye', run: () => { location.href = '/curation' } },
      { label: 'Frame Analyser', icon: 'bi-film', run: () => { location.href = '/frames' } },
      ...(user ? [{ label: 'Account', icon: 'bi-person', run: () => { location.href = '/account' } }] : []),
      ]
    },
  })
  ui.palette.register({
    group: 'Actions',
    items: async () => [
      { label: 'Use dark theme', icon: 'bi-moon', run: () => ui.theme.set('dark') },
      { label: 'Use light theme', icon: 'bi-sun', run: () => ui.theme.set('light') },
      { label: 'Match system theme', icon: 'bi-circle-half', run: () => ui.theme.set('system') },
      ...((await signedInUser()) ? [{ label: 'Sign out', icon: 'bi-box-arrow-right', run: () => { location.href = '/logout' } }] : []),
    ],
  })

  document.addEventListener('keydown', event => {
    if ((event.metaKey || event.ctrlKey) && !event.altKey && (event.key || '').toLowerCase() === 'k') {
      if (document.body.classList.contains("of-bare")) return
      const dialog = document.getElementById('of-palette')
      if (dialog && dialog.open) { event.preventDefault(); ui.palette.close(); return }
      if (event.target && event.target.closest && event.target.closest('input, textarea, select, [contenteditable]')) return
      event.preventDefault()
      ui.palette.open()
    }
  })
  document.addEventListener('click', event => {
    const opener = event.target.closest('[data-palette-open]')
    if (opener) { event.preventDefault(); ui.palette.open() }
  })
  document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('of-palette-input')
    const dialog = document.getElementById('of-palette')
    if (input && dialog) {
      input.addEventListener('input', () => {
        clearTimeout(debounce)
        debounce = setTimeout(async () => { debounce = null; const query = input.value; const groups = await gather(query); if (groups) renderPalette(groups, query) }, 120)
      })
      input.addEventListener('keydown', event => {
        if (event.key === 'ArrowDown') { event.preventDefault(); select(cursor + 1) }
        else if (event.key === 'ArrowUp') { event.preventDefault(); select(cursor - 1) }
        else if (event.key === 'Enter') {
          event.preventDefault()
          const query = input.value
          if (debounce || lastRenderedQuery !== query) {
            clearTimeout(debounce); debounce = null
            gather(query).then(groups => { if (!groups) return; renderPalette(groups, query); runResult(0) })
          } else runResult(cursor)
        }
      })
      dialog.addEventListener('click', event => { if (event.target === dialog) ui.palette.close() })
    }
    const avatar = document.getElementById('rail-avatar')
    const menu = document.getElementById('user-menu')
    if (avatar && menu) avatar.addEventListener('click', () => ui.popover(avatar, menu))
  })
})()

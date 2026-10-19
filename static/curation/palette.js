/* Curation command palette providers: project x view jumps, images by name, curation actions.
 * Loaded after editor.js; relies on curation.js globals (state, request, switchProject, setView,
 * openSource, canDo, checkMissingImages, byId). Every navigation awaits the unsaved-edits guard. */
(() => {
  const palette = window.OF && window.OF.ui && window.OF.ui.palette
  if (!palette) return

  const VIEWS = [
    // view, the tab's label, its icon, and other words people search by (matched, not shown).
    ['source', 'Source images', 'bi-bounding-box', 'points review'], ['crop', 'Crop grid', 'bi-grid-3x3-gap', 'crops patches'],
    ['quality', 'Quality review', 'bi-eye', 'blur'], ['status', 'Dataset status', 'bi-bar-chart', 'missing stats'],
    ['matrix', 'Confusion matrix', 'bi-grid', 'evaluation'], ['versions', 'Versions', 'bi-layers', 'history publish releases'], ['activity', 'Activity', 'bi-people', 'online who presence team'],
  ]

  async function goTo(projectId, view) {
    if (projectId && projectId !== state.projectId && !(await switchProject(projectId))) return false
    return setView(view)
  }

  palette.register({
    group: 'Projects',
    limit: 12,
    items: () => (state.projects || []).flatMap(project => VIEWS.map(([view, label, icon, keywords]) => ({
      label: `${project.name} · ${label}`,
      icon,
      keywords,
      run: () => goTo(project.id, view),
    }))),
  })

  palette.register({
    group: 'Images',
    filter: false,
    items: async query => {
      const q = (query || '').trim()
      if (q.length < 3 || !state.projectId) return []
      const page = await request(`/api/curation/projects/${state.projectId}/source-images?image_search=${encodeURIComponent(q)}&limit=8&source_missing=any`)
      return (page.images || []).map(image => ({
        label: image.image_name || String(image.source_image_uri || '').split('/').pop(),
        hint: [image.region, image.survey_year].filter(Boolean).join(' · '),
        icon: 'bi-image',
        run: () => openSource(image.first_item_id, state.view),
      }))
    },
  })

  palette.register({
    group: 'Curation',
    items: () => {
      const items = []
      if (state.projectId && canDo('project-curator')) {
        items.push({
          label: 'Check for missing images',
          icon: 'bi-arrow-repeat',
          run: async () => { if (await setView('status')) await checkMissingImages() },
        })
      }
      items.push(
        {
          label: 'Toggle Hide missing images',
          icon: 'bi-eye-slash',
          run: () => { const box = byId('filter-hide-missing'); box.checked = !box.checked; box.dispatchEvent(new Event('change', { bubbles: true })) },
        },
        { label: 'Open dataset status', icon: 'bi-clipboard-data', run: () => setView('status') },
      )
      // The commit pill is shown only while there are uncommitted changes.
      if (!byId('commit-pill').classList.contains('d-none')) items.splice(items.length - 1, 0, { label: 'Commit changes', icon: 'bi-check2-circle', run: () => byId('commit-pill').click() })
      return items
    },
  })
})()

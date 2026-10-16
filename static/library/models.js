/** Model picker shared by the collections pages. */
import { el, clear } from './ui.js';
import { api } from './api.js';

export async function loadModels() {
  try {
    return (await api.models()).models || [];
  } catch {
    return [];
  }
}

/**
 * The server decides which models exist; the browser only ever sends one of
 * these back. A model reference names a pickle that gets unpickled on load,
 * so a free-text field here would be a code-execution hole.
 */
export function fillModelSelect(select, models, selected, { blankLabel, collectionWide = false } = {}) {
  clear(select);
  select.append(el('option', { value: '', text: blankLabel || 'default model' }));
  for (const model of models) {
    // SAM 3 is selection-only, so it is not offered where the action would
    // run over a whole collection.
    if (collectionWide && model.selection_only) continue;
    const suffix = !model.usable ? ' — weights missing'
      : (model.cached ? '' : ' (downloads on first use)');
    const option = el('option', {
      value: model.ref,
      text: `${model.label}${suffix}`,
      title: model.note || '',
    });
    if (!model.usable) option.disabled = true;
    if (model.ref === selected) option.selected = true;
    select.append(option);
  }
}

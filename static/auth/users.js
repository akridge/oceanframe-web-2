/** Admin: list, add, change role, (de)activate, reset password. */
import { apiJson, authFetch, whoami } from './session.js';

const rows = document.getElementById('user-rows');
const createForm = document.getElementById('create-form');
let roles = ['viewer', 'curator', 'lead', 'admin'];
let meId = null;

function status(text, isError = false) {
  const node = document.getElementById('users-status');
  node.textContent = text;
  node.classList.toggle('is-error', isError);
  if (text && window.OF && window.OF.ui) window.OF.ui.toast(text, { kind: isError ? "danger" : "ok" });
}

function cell(content) {
  const td = document.createElement('td');
  if (content instanceof Node) td.append(content); else td.textContent = content ?? '';
  return td;
}

function when(iso) {
  return iso ? new Date(iso).toLocaleString() : 'never';
}

async function patch(user, change) {
  try {
    await apiJson(`/api/auth/admin/users/${user.id}`, change, 'PATCH');
    status(`Updated ${user.username}.`);
  } catch (err) {
    status(err.message, true);
  }
  await load();
}

function row(user) {
  const tr = document.createElement('tr');
  if (!user.is_active) tr.className = 'is-inactive';

  const who = document.createElement('div');
  const strong = document.createElement('strong');
  strong.textContent = user.label;
  const sub = document.createElement('div');
  sub.className = 'text-muted text-sm';
  sub.textContent = [user.username, user.email].filter(Boolean).join(' · ');
  who.append(strong, sub);

  const select = document.createElement('select');
  select.className = 'form-select form-select-sm';
  for (const r of roles) select.add(new Option(r, r, false, r === user.role));
  select.addEventListener('change', () => patch(user, { role: select.value }));

  const state = user.is_active ? (user.has_password ? 'active' : 'no password') : 'deactivated';

  const actions = document.createElement('div');
  actions.className = 'user-actions';
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'btn btn-sm';
  toggle.textContent = user.is_active ? 'Deactivate' : 'Activate';
  toggle.disabled = user.id === meId && user.is_active;
  toggle.addEventListener('click', () => patch(user, { is_active: !user.is_active }));
  const reset = document.createElement('button');
  reset.type = 'button';
  reset.className = 'btn btn-sm btn-ghost';
  reset.textContent = 'Set password…';
  reset.addEventListener('click', () => setPassword(user));
  actions.append(toggle, reset);

  tr.append(cell(who), cell(select), cell(state), cell(when(user.last_login)), cell(actions));
  return tr;
}

async function setPassword(user) {
  // The value never leaves this request.
  const value = await window.OF.ui.prompt({
    title: `New password for ${user.username}`,
    message: "8+ characters. They will be signed out everywhere.",
    label: "New password",
    type: "password",
    confirmLabel: "Set password",
    validate: v => (v.length >= 8 ? null : "Use at least 8 characters."),
  });
  if (!value) return;
  try {
    await apiJson(`/api/auth/admin/users/${user.id}/reset-password`, { new_password: value });
    status(`Password set for ${user.username}.`);
  } catch (err) {
    status(err.message, true);
  }
  await load();
}

async function load() {
  const res = await authFetch('/api/auth/admin/users');
  if (!res.ok) {
    rows.replaceChildren();
    status(res.status === 403 ? 'Only admins can manage people.' : `Could not load users (${res.status}).`, true);
    return;
  }
  const body = await res.json();
  roles = body.roles;
  rows.replaceChildren(...body.users.map(row));
  if (!body.users.length) {
    const tr = document.createElement('tr');
    tr.append(cell('Nobody yet — add the first person below.'));
    rows.append(tr);
  }
}

createForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(createForm));
  try {
    await apiJson('/api/auth/admin/users', data);
    status(`Added ${data.username}. Give them their password; they can change it under Account.`);
    createForm.reset();
    createForm.role.value = 'curator';
  } catch (err) {
    status(err.message, true);
  }
  await load();
});

async function init() {
  const me = await whoami();
  meId = me && me.user ? me.user.id : null;
  const select = document.getElementById('create-role');
  for (const r of roles) select.add(new Option(r, r, false, r === 'curator'));
  await load();
}

init();

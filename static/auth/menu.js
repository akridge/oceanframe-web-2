/** Rail user menu: who is signed in, account, people (admins), theme, sign out, credit.
 *  Fills #user-menu (the avatar popover). Pages without the shell (no #rail-avatar) are left alone. */
const V = window.OF_ASSET_V ? '?v=' + encodeURIComponent(window.OF_ASSET_V) : '';

const avatar = document.getElementById('rail-avatar');
const host = document.getElementById('user-menu');

function item(tag, text, icon) {
  const el = document.createElement(tag);
  el.className = 'menu-item';
  el.setAttribute('role', 'menuitem');
  if (icon) {
    const i = document.createElement('i');
    i.className = `bi ${icon}`;
    i.setAttribute('aria-hidden', 'true');
    el.append(i, ' ');
  }
  el.append(document.createTextNode(text));
  return el;
}

function link(href, text, icon) {
  const a = item('a', text, icon);
  a.href = href;
  return a;
}

function initialsOf(user) {
  const label = String(user.initials || user.label || user.username || '').trim();
  const parts = label.split(/\s+/).filter(Boolean);
  const text = user.initials || (parts.length > 1 ? parts[0][0] + parts[1][0] : label.slice(0, 1));
  return String(text).slice(0, 2).toUpperCase();
}

function themeRow() {
  const row = document.createElement('div');
  row.className = 'theme-row';
  const label = document.createElement('span');
  label.className = 'menu-label';
  label.textContent = 'Theme';
  const group = document.createElement('div');
  group.className = 'segmented';
  group.setAttribute('role', 'group');
  group.setAttribute('aria-label', 'Theme');
  const ui = () => (window.OF && window.OF.ui && window.OF.ui.theme) || null;
  const buttons = [['dark', 'Dark'], ['light', 'Light'], ['system', 'System']].map(([value, text]) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = text;
    b.dataset.theme = value;
    b.addEventListener('click', () => { const t = ui(); if (t) { t.set(value); mark(); } });
    return b;
  });
  function mark() {
    const t = ui();
    const current = t ? t.get() : 'system';
    buttons.forEach(b => b.setAttribute('aria-pressed', String(b.dataset.theme === current)));
  }
  group.append(...buttons);
  row.append(label, group);
  mark();
  return row;
}

function credit() {
  const row = document.createElement('div');
  row.className = 'credit';
  const text = document.createElement('span');
  text.textContent = 'an app by';
  const img = document.createElement('img');
  img.src = '/static/img/optics-si-logo.png' + V;
  img.alt = 'OPTICS SI';
  row.append(text, img);
  return row;
}

function sep() {
  const s = document.createElement('div');
  s.className = 'menu-sep';
  return s;
}

async function render() {
  if (!host || !avatar) return;
  let me = null;
  try { const { whoami } = await import('./session.js' + V); me = await whoami(); } catch (_) { /* leave the menu to theme and credit */ }
  const user = me && me.auth_enabled && me.user ? me.user : null;
  const parts = [];

  if (user) {
    const head = document.createElement('div');
    head.className = 'user-head';
    head.title = `Signed in as ${user.username}`;
    const name = document.createElement('strong');
    name.textContent = user.label;
    const role = document.createElement('small');
    role.textContent = user.role;
    head.append(name, role);
    parts.push(head, link('/account', 'Account', 'bi-person'));
    if (user.role === 'admin') {
      parts.push(link('/users', 'Users', 'bi-people'));
      const rail = document.getElementById('rail-users');
      if (rail) rail.hidden = false;
    }
    parts.push(sep());
    avatar.textContent = initialsOf(user);
  }

  parts.push(themeRow());
  if (user) parts.push(sep(), link('/logout', 'Sign out', 'bi-box-arrow-right'));
  parts.push(sep(), credit());
  host.replaceChildren(...parts);
}

if (avatar) render();
